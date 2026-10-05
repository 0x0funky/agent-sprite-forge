"""codeart2d rig_animate.py: easing, sampling, FK, two-bone IK with the ground constraint, exact
shape extents, the paint cascade, data-z, swaps, the lints, and the hero acceptance (walk 8 +
idle 4 on both routes). Every rig here is code-authored and synthetic; the hero is the example
in skills/codeart2d/examples (code-drawn for this module, no image model).
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (
    REPO_ROOT, SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, require_resvg, run_cli, script_path,
)

rig = load_script("codeart2d", "rig_animate")
core = rig.core
SCRIPT = script_path("codeart2d", "rig_animate")
EXAMPLES = REPO_ROOT / "skills" / "codeart2d" / "examples"
HERO_RIG = EXAMPLES / "hero.rig.svg"
HERO_ANIM = EXAMPLES / "hero.anim.json"
SCHEMA_DIR = SKILLS_DIR / "codeart2d" / "references" / "schemas"

# Schema additions requested in handoff/B19-codeart-rig-fx.md section 5 (codeart.schema.json): the IK block
# and the other optional fields rig_animate reads, and the rig-report.json document. Applied in memory here.
POINT_KEYS = {"type": "array", "minItems": 1, "items": {
    "type": "array", "prefixItems": [{"type": "number", "minimum": 0, "maximum": 1},
                                     {"$ref": "common.schema.json#/$defs/point2"}], "minItems": 2, "maxItems": 2}}
NUMBER_KEYS = {"type": "array", "minItems": 1, "items": {
    "type": "array", "prefixItems": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "number"}],
    "minItems": 2, "maxItems": 2}}
HEX = {"$ref": "common.schema.json#/$defs/hexColor"}
CODEART_ADDITIONS = {
    "$defs": {
        "ikGait": {
            "description": "Planted-foot gait for a loop clip with stride_world_units: the foot is planted for the "
                           "stance fraction of the cycle and travels stride * stance against the body, so it stays "
                           "still in the world; during the swing it lifts by lift px and rolls by roll degrees.",
            "type": "object",
            "properties": {"phase": {"type": "number"}, "stance": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                           "lift": {"type": "number", "minimum": 0}, "roll": {"type": "number"},
                           "x": {"type": "number"}}},
        "ikChain": {
            "description": "Two-bone IK: bones [root, middle]; end is a child bone of middle (its pivot is the "
                           "effector) or a rest point [x, y]; pole is the direction the middle joint bends toward; "
                           "exactly one of target (canvas pixels) or gait; angle is the end bone's world angle; "
                           "ground (default true) keeps the end bone's slots on the rig ground line.",
            "type": "object", "required": ["bones"],
            "properties": {
                "bones": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 2, "maxItems": 2},
                "end": {"anyOf": [{"type": "string", "minLength": 1}, {"$ref": "common.schema.json#/$defs/point2"}]},
                "pole": {"$ref": "common.schema.json#/$defs/point2"}, "target": POINT_KEYS, "angle": NUMBER_KEYS,
                "gait": {"$ref": "#/$defs/ikGait"}, "ground": {"type": "boolean"}, "ease": {"$ref": "#/$defs/easing"}},
            "oneOf": [{"required": ["target"]}, {"required": ["gait"]}]},
        "rig_report_v1": {
            "description": "rig-report.json written by rig_animate.py: per-clip frame records (QA metrics, ground "
                           "edge, margin, IK contacts), seam reports, planted-foot drift, entry frame, stride and "
                           "events, all in output pixels; qa is the envelope of codeart-meta.json.",
            "type": "object", "required": ["schema", "route", "frame_size", "anchor_px", "clips", "qa"],
            "properties": {
                "schema": {"const": "codeart2d.rig_report.v1"}, "route": {"enum": ["pixel", "vector"]},
                "frame_size": {"$ref": "common.schema.json#/$defs/size2"},
                "anchor_px": {"$ref": "common.schema.json#/$defs/point2"}, "ground_px": {"type": "number"},
                "zoom": {"type": "integer", "minimum": 1},
                "clips": {"type": "object", "minProperties": 1, "additionalProperties": {
                    "type": "object", "required": ["frames", "loop", "duration_ms", "entry_frame", "contact"],
                    "properties": {
                        "frames": {"type": "array", "minItems": 1, "items": {
                            "type": "object", "required": ["id", "t", "file", "stored_as", "qa", "ground"],
                            "properties": {"file": {"$ref": "common.schema.json#/$defs/relPath"}}}},
                        "loop": {"type": "boolean"}, "duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
                        "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]},
                        "entry_frame": {"type": "integer", "minimum": 0}, "contact": {"type": "object"},
                        "events": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipEvent"}}}}},
                "ik_clamps": {"type": "array"}, "ground_failures": {"type": "array"},
                "margin_failures": {"type": "array"}, "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}}},
    },
    "rigClip_properties": {
        "ik": {"type": "object", "additionalProperties": {"$ref": "#/$defs/ikChain"}},
        "grounded": {"type": "boolean"}, "entry_frame": {"type": "integer", "minimum": 0},
        "transitions": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipTransition"}},
        "role": {"enum": ["player", "enemy", "npc", "fx", "prop"]}},
    "rig_anim_v1_properties": {
        "states": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
        "entry_reference": {"type": "string", "minLength": 1},
        "pixel": {"type": "object", "properties": {
            "ramps": {"type": "object", "additionalProperties": {"anyOf": [
                {"type": "array", "items": HEX, "minItems": 5, "maxItems": 5},
                {"type": "object", "required": ["mid"],
                 "properties": {key: HEX for key in ("hi", "mid", "lo", "dark", "out")}}]}},
            "light": {"anyOf": [{"$ref": "common.schema.json#/$defs/point2"}, {"type": "null"}]},
            "inner_lines": {"type": "boolean"}, "outline": {"type": "string", "minLength": 1},
            "outline_mode": {"enum": ["solid", "selout", "none"]}}}},
}


def patched_validator(definition: str):
    """Draft 2020-12 validator for codeart.schema.json#/$defs/<definition> with CODEART_ADDITIONS applied."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    schemas = {path.name.removesuffix(".schema.json"): json.loads(path.read_text(encoding="utf-8"))
               for path in SCHEMA_DIR.glob("*.schema.json")}
    codeart = schemas["codeart"]
    codeart["$defs"].update(copy.deepcopy(CODEART_ADDITIONS["$defs"]))
    codeart["$defs"]["rigClip"]["properties"].update(copy.deepcopy(CODEART_ADDITIONS["rigClip_properties"]))
    codeart["$defs"]["rig_anim_v1"]["properties"].update(copy.deepcopy(CODEART_ADDITIONS["rig_anim_v1_properties"]))
    registry = Registry().with_resources((schema["$id"], DRAFT202012.create_resource(schema))
                                         for schema in schemas.values())
    return Draft202012Validator({"$ref": f"{codeart['$id']}#/$defs/{definition}"}, registry=registry)


def assert_patched_valid(document, definition: str) -> None:
    errors = [f"{error.json_path}: {error.message}" for error in patched_validator(definition).iter_errors(document)]
    assert not errors, "\n".join(errors)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- synthetic rigs

LEG_RIG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" data-anchor="16 29">
  <style>.c-body{fill:#3b5dc9}.c-leg{fill:#4a5a85}.c-boot{fill:#5a3a2a}.c-out{stroke:#1a1c2c;stroke-width:1}</style>
  <g id="root" data-pivot="16 29">
    <g id="hips" data-pivot="16 18">
      <rect data-z="20" class="c-body c-out" x="12" y="7" width="8" height="12" rx="2"/>
      <g id="thigh" data-pivot="16 18" data-z="10">
        <rect class="c-leg c-out" x="14.5" y="17" width="3" height="7" rx="1"/>
        <g id="shin" data-pivot="16 23">
          <rect class="c-leg c-out" x="14.5" y="22.5" width="3" height="6" rx="1"/>
          <g id="foot" data-pivot="16 28">
            <rect class="c-boot c-out" x="14.5" y="27" width="5" height="2" rx="0.5"/>
          </g>
        </g>
      </g>
    </g>
  </g>
</svg>"""
LEG_IK = {"bones": ["thigh", "shin"], "end": "foot", "pole": [1, 0]}


def leg_anim(**clip) -> dict:
    base = {"frames": 4, "loop": True, "duration_ms": 120,
            "tracks": {"hips": {"translate": [[0, [0, 0]], [0.25, [0, 1]], [0.5, [0, 2]], [0.75, [0, 1]], [1, [0, 0]]]}},
            "ik": {"leg": dict(LEG_IK, target=[[0, [16, 40]]])}}
    base.update(clip)
    return {"schema": "codeart2d.rig_anim.v1", "rig": "leg.rig.svg", "clips": {"stand": base}}


def write_case(directory: Path, svg: str = LEG_RIG, anim: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "leg.rig.svg").write_text(svg, encoding="utf-8")
    path = directory / "leg.anim.json"
    path.write_text(json.dumps(anim or leg_anim()), encoding="utf-8")
    return path


def run_rig(anim: Path, output: Path, *extra: str):
    return run_cli([SCRIPT, "--anim", anim, "--output-dir", output, *extra])


def report_checks(output: Path) -> dict:
    report = read_json(output / "rig-report.json")
    return {item["id"]: item for item in report["qa"]["checks"]}


# ----------------------------------------------------------------------------- CLI conventions

def test_help_cp1252_and_cp950():
    assert_cli_help("codeart2d", "rig_animate")


def test_refuses_an_existing_output_dir(tmp_path):
    anim = write_case(tmp_path / "case")
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("mine", encoding="utf-8")
    result = run_rig(anim, output)
    assert result.returncode == 1
    assert result.stderr.startswith("error: refusing to replace existing output")
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]


@pytest.mark.resvg
def test_strict_qc_failure_publishes_nothing(tmp_path):
    require_resvg()
    edge_rig = LEG_RIG.replace('x="12" y="7" width="8"', 'x="0" y="7" width="20"')  # the body touches x = 0
    anim = write_case(tmp_path / "case", edge_rig)
    output = tmp_path / "out"
    result = run_rig(anim, output, "--strict-qc")
    assert result.returncode == 1, result.stdout
    assert result.stderr.startswith("error: strict QC failed (") and "margins" in result.stderr
    assert "nothing was published" in result.stderr
    assert not output.exists()
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".out.stage-")]


# ----------------------------------------------------------------------------- easing and sampling

def test_named_easings():
    values = {name: rig.parse_ease(name) for name in ("linear", "sine", "in", "out", "step")}
    assert values["linear"](0.3) == pytest.approx(0.3)
    assert values["sine"](0.5) == pytest.approx(0.5) and values["sine"](0.25) == pytest.approx(0.5 - 0.5 * math.cos(math.pi / 4))
    assert values["in"](0.5) == pytest.approx(0.25) and values["out"](0.5) == pytest.approx(0.75)
    assert values["step"](0.999) == 0.0 and values["step"](1.0) == 1.0
    for function in values.values():
        assert function(0.0) == pytest.approx(0.0) and function(1.0) == pytest.approx(1.0)
    with pytest.raises(core.CodeArtError, match="unknown ease"):
        rig.parse_ease("bounce")


def test_cubic_bezier_matches_a_dense_reference():
    """CSS 'ease' = cubic-bezier(.25,.1,.25,1), checked against a dense sampling of the curve."""
    ease = rig.parse_ease("cubic-bezier(0.25, 0.1, 0.25, 1)")
    s = np.linspace(0.0, 1.0, 200001)
    x = 3 * (1 - s) ** 2 * s * 0.25 + 3 * (1 - s) * s ** 2 * 0.25 + s ** 3
    y = 3 * (1 - s) ** 2 * s * 0.1 + 3 * (1 - s) * s ** 2 * 1.0 + s ** 3
    for u in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert ease(u) == pytest.approx(float(np.interp(u, x, y)), abs=1e-6)
    overshoot = rig.parse_ease("cubic-bezier(0.3, -0.5, 0.7, 1.5)")
    assert min(overshoot(u / 100) for u in range(101)) < 0.0 < 1.0 < max(overshoot(u / 100) for u in range(101))
    with pytest.raises(core.CodeArtError, match=r"x1 and x2 must lie in \[0, 1\]"):
        rig.parse_ease("cubic-bezier(1.2, 0, 0.5, 1)")


def test_spring_ends_exactly_and_damping_controls_overshoot():
    for spec in ("spring(120, 8)", "spring(100, 20)", "spring(50, 40)"):
        ease = rig.parse_ease(spec)
        assert ease(0.0) == pytest.approx(0.0, abs=1e-12) and ease(1.0) == pytest.approx(1.0, abs=1e-12)
    underdamped = [rig.parse_ease("spring(120, 8)")(u / 200) for u in range(201)]
    assert max(underdamped) > 1.05  # d*d < 4k overshoots
    for spec in ("spring(100, 20)", "spring(50, 40)"):  # critical and over-damped approach monotonically
        values = [rig.parse_ease(spec)(u / 200) for u in range(201)]
        assert all(b >= a - 1e-12 for a, b in zip(values, values[1:])) and max(values) <= 1.0 + 1e-9
    with pytest.raises(core.CodeArtError, match="k > 0 and d > 0"):
        rig.parse_ease("spring(10, 0)")


def test_loop_and_oneshot_sampling():
    assert rig.clip_times(8, True) == [i / 8 for i in range(8)]  # the wrap frame is never repeated
    assert rig.clip_times(5, False) == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert rig.clip_times(1, False) == [0.0]


def test_sample_keys_eases_segments_and_holds_switches():
    keys = [(0.0, 0.0), (0.5, 10.0), (1.0, 0.0)]
    assert rig.sample_keys(keys, 0.25, rig.parse_ease("linear")) == pytest.approx(5.0)
    assert rig.sample_keys(keys, 0.25, rig.parse_ease("in")) == pytest.approx(2.5)
    assert rig.sample_keys([(0.2, 3.0)], 0.0, rig.parse_ease("linear")) == 3.0  # before the first key
    swaps = [(0.0, "open"), (0.5, "fist")]
    assert [rig.sample_keys(swaps, t, rig.parse_ease("linear"), hold=True) for t in (0.0, 0.49, 0.5, 1.0)] == \
        ["open", "open", "fist", "fist"]


# ----------------------------------------------------------------------------- FK, IK and geometry

ARM_RIG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32" data-anchor="16 30">
  <g id="upper" data-pivot="10 10"><rect x="9" y="9" width="2" height="8" fill="#ff0000"/>
    <g id="lower" data-pivot="10 16"><rect x="9" y="15" width="2" height="6" fill="#00ff00"/></g></g>
</svg>"""


def test_fk_composes_parent_translate_pivot_rotate_scale():
    arm = rig.Rig(ARM_RIG)
    poses = {"upper": rig.BonePose(rotate=90.0, translate=(2.0, 0.0)), "lower": rig.BonePose(scale=(2.0, 2.0))}
    world = rig.world_matrices(arm, poses)
    # upper: T(2,0) * rotate 90 about (10,10): the lower pivot (10,16) goes to (4+2, 10)
    assert rig.apply(world["upper"], (10, 16)) == pytest.approx((6.0, 10.0))
    # lower scales about its own pivot, then follows its parent: (10,18) is 2 px below the pivot -> 4 px, turned
    assert rig.apply(world["lower"], (10, 18)) == pytest.approx((2.0, 10.0))


def test_two_bone_ik_reaches_or_reports_the_clamp():
    first, second, end, clamp = rig.solve_two_bone((0.0, 0.0), (6.0, 6.0), 5.0, 5.0, side=-1.0)
    knee = (5.0 * math.cos(first), 5.0 * math.sin(first))
    assert end == (6.0, 6.0) and clamp == 0.0
    assert math.dist((0, 0), knee) == pytest.approx(5.0) and math.dist(knee, (6, 6)) == pytest.approx(5.0)
    other = rig.solve_two_bone((0.0, 0.0), (6.0, 6.0), 5.0, 5.0, side=1.0)[0]
    assert math.sin(first - math.atan2(6, 6)) * math.sin(other - math.atan2(6, 6)) < 0  # the two bend sides
    first, second, end, clamp = rig.solve_two_bone((0.0, 0.0), (0.0, 13.0), 5.0, 5.0, side=1.0)
    assert clamp == pytest.approx(3.0) and end == pytest.approx((0.0, 10.0)) and first == pytest.approx(second)


def test_shape_extent_is_exact_for_rotated_curves():
    """The ground constraint needs exact extremes: compare with dense outline sampling."""
    rounded = rig.ET.fromstring('<rect xmlns="http://www.w3.org/2000/svg" x="2" y="3" width="8" height="4" rx="1.5"/>')
    path = rig.ET.fromstring('<path xmlns="http://www.w3.org/2000/svg" '
                             'd="M1 1 C 4 -2 7 9 10 4 Q 12 1 13 6 A 3 2 30 1 1 6 8 Z"/>')
    matrix = rig.rotate(37.0, 5.0, 5.0) @ rig.translate(0.3, -1.2)
    for element, samples in ((rounded, _rect_samples(2, 3, 8, 4, 1.5)), (path, _path_samples())):
        geometry = rig.shape_geometry(element)
        points = np.array([rig.apply(matrix, point) for point in samples])
        for direction in ((0, 1), (1, 0), (-1, 0), (0, -1), (0.6, 0.8)):
            exact = geometry.extent(matrix, direction)
            sampled = float((points @ np.array(direction)).max())
            assert exact >= sampled - 1e-9 and exact - sampled < 2e-3


def _rect_samples(x, y, w, h, r):
    angles = np.linspace(0, math.pi / 2, 400)
    corners = [(x + w - r, y + r, -math.pi / 2), (x + w - r, y + h - r, 0), (x + r, y + h - r, math.pi / 2),
               (x + r, y + r, math.pi)]
    return [(cx + r * math.cos(start + a), cy + r * math.sin(start + a)) for cx, cy, start in corners for a in angles]


def _path_samples():
    s = np.linspace(0, 1, 2000)[:, None]
    cubic = (1 - s) ** 3 * np.array([1, 1]) + 3 * (1 - s) ** 2 * s * np.array([4, -2]) + \
        3 * (1 - s) * s ** 2 * np.array([7, 9]) + s ** 3 * np.array([10, 4])
    quad = (1 - s) ** 2 * np.array([10, 4]) + 2 * (1 - s) * s * np.array([12, 1]) + s ** 2 * np.array([13, 6])
    geometry = rig.path_geometry("M13 6 A 3 2 30 1 1 6 8")
    cx, cy, rx, ry, phi, start, sweep = geometry.arcs[0]
    theta = start + sweep * s[:, 0]
    arc = np.stack([cx + rx * np.cos(phi) * np.cos(theta) - ry * np.sin(phi) * np.sin(theta),
                    cy + rx * np.sin(phi) * np.cos(theta) + ry * np.cos(phi) * np.sin(theta)], axis=1)
    assert np.allclose(arc[0], (13, 6)) and np.allclose(arc[-1], (6, 8))  # the arc fit reaches its endpoints
    return [tuple(p) for p in np.concatenate([cubic, quad, arc])]


def test_ground_constraint_lifts_a_rotated_foot_onto_the_ground():
    leg = rig.Rig(LEG_RIG)
    for clearance in (1.0, 0.5):
        poser = rig.Poser(leg, clearance=lambda slot, c=clearance: c)
        for angle in (0.0, 25.0, -30.0):
            anim = rig.Animation(leg_anim(ik={"leg": dict(LEG_IK, target=[[0, [16, 40]]], angle=[[0, angle]])}),
                                 leg)
            frame = poser.pose(anim.clips["stand"], 0, 0.0)
            contact = frame.contacts["leg"]
            assert contact["planted"] and contact["clamp_px"] == 0.0
            assert contact["lowest_y"] == pytest.approx(29.0, abs=1e-9)  # outline edge exactly on the ground
            foot = leg.slots[-1]
            lowest = foot.geometry.extent(frame.world["foot"] @ foot.local, (0, 1))
            assert lowest + clearance == pytest.approx(29.0, abs=1e-9)
            assert rig.matrix_angle(frame.world["foot"]) == pytest.approx(angle, abs=1e-9)


def test_ik_follows_a_rotated_and_moved_parent():
    """The chain root's parent may rotate and translate: the ankle still lands on the target and the foot keeps
    its world angle."""
    leg = rig.Rig(LEG_RIG)
    poser = rig.Poser(leg, clearance=lambda slot: 1.0)
    anim = rig.Animation(leg_anim(tracks={"hips": {"rotate": [[0, 20]], "translate": [[0, [1, 1]]]}},
                                  ik={"leg": dict(LEG_IK, target=[[0, [18, 26]]], angle=[[0, 10]], ground=False)}),
                         leg)
    frame = poser.pose(anim.clips["stand"], 0, 0.0)
    assert rig.apply(frame.world["foot"], (16, 28)) == pytest.approx((18.0, 26.0), abs=1e-9)
    assert rig.matrix_angle(frame.world["foot"]) == pytest.approx(10.0, abs=1e-9)
    assert rig.matrix_angle(frame.world["hips"]) == pytest.approx(20.0)
    assert frame.contacts["leg"]["planted"] is False and frame.contacts["leg"]["clamp_px"] == 0.0


def test_ik_with_an_end_point_instead_of_an_end_bone():
    arm = rig.Rig(ARM_RIG)
    anim = rig.Animation({"schema": "codeart2d.rig_anim.v1", "rig": "x", "clips": {"reach": {
        "frames": 1, "loop": False, "duration_ms": 100,
        "ik": {"arm": {"bones": ["upper", "lower"], "end": [10, 21], "pole": [-1, 0], "target": [[0, [14, 18]]],
                       "ground": False}}}}}, arm)
    frame = rig.Poser(arm, clearance=lambda slot: 0.0).pose(anim.clips["reach"], 0, 0.0)
    assert rig.apply(frame.world["lower"], (10, 21)) == pytest.approx((14.0, 18.0), abs=1e-9)
    elbow = rig.apply(frame.world["upper"], (10, 16))
    assert math.dist((10, 10), elbow) == pytest.approx(6.0) and math.dist(elbow, (14, 18)) == pytest.approx(5.0)
    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    reach, bend = (4, 8), (elbow[0] - 10, elbow[1] - 10)
    assert cross(reach, bend) * cross(reach, (-1, 0)) > 0 and elbow[0] < 10  # bent toward the pole (-x)


def test_draw_order_follows_data_z_not_the_hierarchy():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" data-anchor="8 15">
      <g id="body" data-pivot="8 8" data-z="30"><rect id="torso" x="5" y="3" width="6" height="8" fill="#3b5dc9"/>
        <g id="arm" data-pivot="8 4" data-z="60"><rect id="hand" x="7" y="4" width="2" height="6" fill="#f0b98c"/></g>
      </g>
      <g id="leg" data-pivot="8 11" data-z="50"><rect id="shin" x="6" y="10" width="2" height="5" fill="#4a5a85"/></g>
    </svg>"""
    figure = rig.Rig(svg)
    frame = rig.Frame("x", 0, 0.0, rig.world_matrices(figure, {}), {}, set(), {}, [])
    assert [slot.name for slot in rig.visible_slots(figure, frame)] == ["torso", "shin", "hand"]


def test_swap_and_show_tracks_pick_slot_variants():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" data-anchor="8 15">
      <g id="hand" data-pivot="8 8"><rect data-variant="open" id="open" x="6" y="6" width="4" height="4" fill="#f0b98c"/>
        <g data-variant="fist"><rect id="fist" x="7" y="7" width="2" height="2" fill="#c8845e"/></g></g>
      <g id="charm" data-pivot="8 12"><rect id="charm-shape" x="7" y="12" width="2" height="2" fill="#ffcd75"/></g>
    </svg>"""
    figure = rig.Rig(svg)
    assert figure.bones["hand"].variants == ("open", "fist")
    anim = rig.Animation({"schema": "codeart2d.rig_anim.v1", "rig": "x", "clips": {"grab": {
        "frames": 2, "loop": False, "duration_ms": 100,
        "tracks": {"hand": {"swap": [[0, "open"], [1, "fist"]]}, "charm": {"show": [[0, True], [1, False]]}}}}},
        figure)
    poser = rig.Poser(figure, clearance=lambda slot: 0.0)
    names = [[slot.name for slot in rig.visible_slots(figure, poser.pose(anim.clips["grab"], i, t))]
             for i, t in enumerate(anim.clips["grab"].times())]
    assert names == [["open", "charm-shape"], ["fist"]]
    with pytest.raises(core.CodeArtError, match="no variant 'claw'"):
        rig.Animation({"schema": "codeart2d.rig_anim.v1", "rig": "x", "clips": {"grab": {
            "frames": 1, "loop": False, "duration_ms": 100, "tracks": {"hand": {"swap": [[0, "claw"]]}}}}}, figure)


def test_paint_cascade_classes_inheritance_and_inline_style():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" data-anchor="8 15"
               fill="#111111"><style>.c-out{stroke:#1a1c2c;stroke-width:2}.c-red{fill:#ff0000} rect.c-red{fill:#aa0000}</style>
      <g id="a" data-pivot="0 0" class="c-out" color="#00ff00">
        <rect id="r1" class="c-red" x="1" y="1" width="2" height="2"/>
        <rect id="r2" x="1" y="1" width="2" height="2" fill="currentColor"/>
        <rect id="r3" class="c-red" style="fill:#0000ff" x="1" y="1" width="2" height="2"/>
        <circle id="c1" cx="5" cy="5" r="1"/>
      </g></svg>"""
    paints = {slot.name: slot.paint for slot in rig.Rig(svg).slots}
    assert paints["r1"]["fill"] == "#aa0000"  # the more specific rect.c-red wins
    assert paints["r2"]["fill"] == "#00ff00"  # currentColor follows the inherited color
    assert paints["r3"]["fill"] == "#0000ff"  # the style attribute beats every rule
    assert paints["c1"]["fill"] == "#111111"  # inherited from the root
    assert all(paint["stroke"] == "#1a1c2c" and paint["stroke_width"] == 2.0 for paint in paints.values())
    with pytest.raises(core.CodeArtError, match="unsupported selector"):
        rig.Rig(svg.replace("rect.c-red", "#a rect"))


# ----------------------------------------------------------------------------- lints: each fires on its fixture

def test_lint_nan_in_the_animation(tmp_path):
    anim = write_case(tmp_path / "case")
    anim.write_text(anim.read_text(encoding="utf-8").replace('"duration_ms": 120', '"duration_ms": 120, '
                                                                                   '"stride_world_units": NaN'),
                    encoding="utf-8")
    result = run_rig(anim, tmp_path / "out")
    assert result.returncode == 1 and "NaN is not allowed" in result.stderr
    assert not (tmp_path / "out").exists()


def test_lint_complex_number_in_the_rig(tmp_path):
    svg = LEG_RIG.replace('<rect data-z="20"', '<path d="M12 6 L 61.13-0.00j 8 Z" fill="#3b5dc9"/><rect data-z="20"')
    result = run_rig(write_case(tmp_path / "case", svg), tmp_path / "out")
    assert result.returncode == 1 and "complex number" in result.stderr


def test_lint_clip_id_collision(tmp_path):
    clips = '<defs><clipPath id="c"><rect width="4" height="4"/></clipPath><clipPath id="c"><rect width="9" height="9"/>' \
            '</clipPath></defs>'
    result = run_rig(write_case(tmp_path / "case", LEG_RIG.replace("</style>", "</style>" + clips)), tmp_path / "out")
    assert result.returncode == 1 and "duplicate id 'c'" in result.stderr


@pytest.mark.resvg
def test_frames_batched_in_one_page_never_share_a_clip_id(monkeypatch):
    """Vector frames are id-prefixed per frame: a clipPath defined once in the rig appears once per frame."""
    require_resvg()
    svg = LEG_RIG.replace("</style>", '</style><defs><clipPath id="cut"><rect x="0" y="0" width="32" height="20"/>'
                                      '</clipPath></defs>').replace('<rect data-z="20"', '<rect data-z="20" '
                                                                                         'clip-path="url(#cut)"')
    leg = rig.Rig(svg)
    renderer = rig.Renderer(leg, rig.RouteOptions(route="vector", zoom=1))
    seen = []
    monkeypatch.setattr(core, "rasterize", lambda text, **kw: (seen.append(text) or (np.zeros((32, 32, 4), np.uint8),
                                                                                       {"name": "x", "version": "0"})))
    anim = rig.Animation(leg_anim(), leg)
    poser = rig.Poser(leg, clearance=renderer.clearance)
    for index in range(2):
        renderer.render(poser.pose(anim.clips["stand"], index, index / 4), f"stand-{index:02d}")
    page = "".join(seen)
    ids = [element.get("id") for text in seen for element in rig.ET.fromstring(text).iter() if element.get("id")]
    assert ids == ["stand-00_cut", "stand-01_cut"] and len(set(ids)) == len(ids)
    assert "url(#stand-00_cut)" in seen[0] and "url(#stand-01_cut)" in seen[1] and page.count('id="cut"') == 0


@pytest.mark.resvg
def test_lint_ik_clamp(tmp_path):
    require_resvg()
    anim = write_case(tmp_path / "case", anim=leg_anim(ik={"leg": dict(LEG_IK, target=[[0, [30, 29]]])}))
    output = tmp_path / "out"
    assert run_rig(anim, output).returncode == 0
    checks = report_checks(output)
    assert checks["ik_clamp"]["status"] == "fail" and checks["ik_clamp"]["value"] == 4
    clamp = read_json(output / "rig-report.json")["ik_clamps"][0]
    assert clamp["chain"] == "leg" and clamp["clamp_px"] > 1.0


@pytest.mark.resvg
def test_lint_ground_row(tmp_path):
    """Without IK the rest pose decides contact: the foot fill ends on the ground line (y=29), so the 1 px
    pixel outline goes one row below it (sinking by 1)."""
    require_resvg()
    anim = write_case(tmp_path / "case", anim={"schema": "codeart2d.rig_anim.v1", "rig": "leg.rig.svg",
                                               "clips": {"stand": {"frames": 2, "loop": True, "duration_ms": 100}}})
    output = tmp_path / "out"
    assert run_rig(anim, output).returncode == 0
    assert report_checks(output)["ground_row"]["status"] == "fail"
    failures = read_json(output / "rig-report.json")["ground_failures"]
    assert [failure["gap_px"] for failure in failures] == [-1, -1]


@pytest.mark.resvg
def test_lint_margins(tmp_path):
    require_resvg()
    anim = write_case(tmp_path / "case", LEG_RIG.replace('x="12" y="7" width="8"', 'x="0" y="7" width="20"'))
    output = tmp_path / "out"
    assert run_rig(anim, output).returncode == 0
    checks = report_checks(output)
    assert checks["margins"]["status"] == "fail" and checks["margins"]["value"] == 4
    assert read_json(output / "rig-report.json")["margin_failures"][0]["margin_px"] == 0


def test_ik_chains_refuse_conflicting_tracks():
    leg = rig.Rig(LEG_RIG)
    with pytest.raises(core.CodeArtError, match="remove its rotate track"):
        rig.Animation(leg_anim(tracks={"shin": {"rotate": [[0, 10]]}}), leg)
    with pytest.raises(core.CodeArtError, match="assumes unscaled bones"):
        rig.Animation(leg_anim(tracks={"hips": {"scale": [[0, 2]]}}), leg)
    with pytest.raises(core.CodeArtError, match="would change the IK bone lengths"):
        rig.Animation(leg_anim(tracks={"foot": {"translate": [[0, [1, 0]]]}}), leg)
    with pytest.raises(core.CodeArtError, match="give pole"):
        rig.Animation(leg_anim(ik={"leg": {"bones": ["thigh", "shin"], "end": "foot", "target": [[0, [16, 30]]]}}),
                      leg)
    with pytest.raises(core.CodeArtError, match="needs a loop clip with stride_world_units"):
        rig.Animation(leg_anim(ik={"leg": dict(LEG_IK, gait={"stance": 0.6})}), leg)


# ----------------------------------------------------------------------------- the hero (plan acceptance)

@pytest.fixture(scope="module")
def hero_pixel(tmp_path_factory):
    require_resvg()
    output = tmp_path_factory.mktemp("hero") / "pixel"
    result = run_rig(HERO_ANIM, output, "--route", "pixel", "--build-clips", "--strict-qc",
                     "--godot-world-height", "1.8")
    assert result.returncode == 0, result.stderr
    return output, json.loads(result.stdout)


@pytest.fixture(scope="module")
def hero_vector(tmp_path_factory):
    require_resvg()
    output = tmp_path_factory.mktemp("hero") / "vector"
    result = run_rig(HERO_ANIM, output, "--route", "vector", "--build-clips", "--strict-qc")
    assert result.returncode == 0, result.stderr
    return output, json.loads(result.stdout)


@pytest.mark.resvg
def test_hero_pixel_route_meets_the_pixel_gates(hero_pixel):
    output, summary = hero_pixel
    assert summary["qa"] == "pass" and summary["clips"] == ["walk", "idle"] and summary["route"] == "pixel"
    report = read_json(output / "rig-report.json")
    frames = [frame for clip in report["clips"].values() for frame in clip["frames"]]
    assert len(frames) == 12
    for frame in frames:
        qa = frame["qa"]
        assert qa["partial_alpha"] == 0 and qa["off_palette"] == 0 and qa["outline_gaps"] == 0, frame["id"]
        assert qa["l_corners"] <= 10, frame["id"]
    for name in ("walk", "idle"):
        assert 0.8 <= report["clips"][name]["seam"]["seam_over_median"] <= 1.25
    checks = {item["id"]: item for item in report["qa"]["checks"]}
    assert checks["ik_foot_drift"]["value"] == 0.0 and checks["ik_clamp"]["value"] == 0
    assert checks["ground_row"]["status"] == "pass" and checks["build_clips"]["status"] == "pass"
    for image in sorted((output / "frames").glob("*.png")):
        with Image.open(image) as png:
            assert png.mode == "RGBA" and png.size == (64, 64)


@pytest.mark.resvg
def test_hero_vector_route_builds_with_zero_drift(hero_vector):
    output, summary = hero_vector
    assert summary["qa"] == "pass" and summary["route"] == "vector"
    report = read_json(output / "rig-report.json")
    assert report["frame_size"] == [256, 256] and report["anchor_px"] == [128.0, 236.0]
    checks = {item["id"]: item for item in report["qa"]["checks"]}
    assert checks["partial_alpha"]["status"] == "skipped"  # anti-aliased by design
    assert checks["ik_foot_drift"]["value"] == 0.0 and checks["ik_clamp"]["value"] == 0
    for name in ("walk", "idle"):
        assert 0.8 <= report["clips"][name]["seam"]["seam_over_median"] <= 1.25
    assert (output / "compiled-clips" / "animation-clips.json").is_file()


@pytest.mark.resvg
def test_hero_planted_foot_pixels_do_not_slide(hero_pixel):
    """IK foot drift 0 px measured on pixels: while leg_f is planted, its boot pixels sit at the same world x
    (canvas x + 3 px of travel per frame: stride 24 over 8 frames)."""
    output, _ = hero_pixel
    walk = read_json(output / "rig-report.json")["clips"]["walk"]
    boot = np.array([0x5A, 0x3A, 0x2A])
    spans = []
    for index, frame in enumerate(walk["frames"]):
        if not frame["contacts"]["leg_f"]["planted"]:
            continue
        with Image.open(output / frame["file"]) as png:
            pixels = np.asarray(png.convert("RGBA"))
        ys, xs = np.nonzero(np.all(pixels[..., :3] == boot, axis=-1) & (pixels[..., 3] > 0))
        spans.append((int(xs.min()) + 3 * index, int(xs.max()) + 3 * index, int(ys.max())))
    assert len(spans) >= 4 and len(set(spans)) == 1


@pytest.mark.resvg
def test_hero_contact_report_stride_events_and_entry_frame(hero_pixel):
    output, _ = hero_pixel
    report = read_json(output / "rig-report.json")
    walk = report["clips"]["walk"]
    assert walk["stride_world_units"] == 24.0 and walk["stride_px_per_frame"] == 3.0
    assert walk["events"] == [{"at": 0, "name": "step_r"}, {"at": 4, "name": "step_l"}]
    contact = walk["contact"]
    assert contact["leg_f"]["drift_x_px"] == 0.0 and contact["leg_b"]["drift_x_px"] == 0.0
    assert len(contact["leg_f"]["planted_frames"]) == 5 and len(contact["leg_b"]["planted_frames"]) == 5
    assert all(any(c["planted"] for c in frame["contacts"].values()) for frame in walk["frames"])  # walk: always a foot down
    assert report["clips"]["idle"]["entry_frame"] == 0 and 0 <= walk["entry_frame"] < 8
    clips = read_json(output / "clips.json")
    assert clips["clips"]["walk"]["entry_frame"] == walk["entry_frame"]
    assert clips["clips"]["walk"]["stride_world_units"] == 24.0 and clips["states"] == {"idle": "idle", "moving": "walk"}


@pytest.mark.resvg
def test_hero_outputs_validate_against_the_contracts(hero_pixel):
    output, _ = hero_pixel
    clips = read_json(output / "clips.json")
    assert_valid_contract(clips, "sprite", "clips_input", skill="codeart2d")
    assert clips["art_source"] == "code" and clips["pixel_art"] is True and clips["sampling"] == "nearest"
    meta = read_json(output / "codeart-meta.json")
    assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
    assert meta["art_source"] == "code" and meta["disclosure"] == "code-drawn, no image model"
    assert_valid_contract(meta["qa"], "common", "qaEnvelope", skill="codeart2d")
    assert {ref["path"] for ref in meta["qa"]["outputs"]} >= {"clips.json"}
    assert {ref["path"].rsplit("/", 1)[-1] for ref in meta["qa"]["inputs"]} == {"hero.rig.svg", "hero.anim.json"}
    built = read_json(output / "compiled-clips" / "animation-clips.json")
    assert_valid_contract(built, "sprite", "animation_clips_v2", skill="codeart2d")
    assert_patched_valid(read_json(output / "rig-report.json"), "rig_report_v1")


def test_hero_example_validates_against_rig_anim_v1():
    anim = read_json(HERO_ANIM)
    assert_valid_contract(anim, "codeart", "rig_anim_v1", skill="codeart2d")
    assert_patched_valid(anim, "rig_anim_v1")
    broken = copy.deepcopy(anim)
    broken["clips"]["walk"]["ik"]["leg_f"]["gait"]["stance"] = 2
    assert not patched_validator("rig_anim_v1").is_valid(broken)
    # The additions are additive: A0's contract fixture stays valid.
    assert_patched_valid(read_json(REPO_ROOT / "tests" / "fixtures" / "contracts" / "codeart.rig_anim_v1.valid.json"),
                         "rig_anim_v1")


@pytest.mark.resvg
def test_hero_identical_poses_are_stored_once(hero_pixel):
    output, summary = hero_pixel
    report = read_json(output / "rig-report.json")
    idle = report["clips"]["idle"]["frames"]
    assert idle[3]["stored_as"] == idle[1]["stored_as"] == "idle-01"  # t=0.25 and t=0.75 are the same pose
    assert summary["frames"] == len(list((output / "frames").glob("*.png"))) == 11
    assert read_json(output / "clips.json")["clips"]["idle"]["frames"] == ["idle-00", "idle-01", "idle-02", "idle-01"]


@pytest.mark.resvg
def test_hero_sprite3d_contracts(hero_pixel):
    output, _ = hero_pixel
    walk = read_json(output / "godot" / "walk.sprite3d.json")
    assert walk["schema"] == "generate2dsprite.godot_sprite3d.v1" and walk["frame_size"] == [64, 64]
    assert walk["sprite3d_offset"] == [0.0, 27.0] and walk["loop"] is True and len(walk["frames"]) == 8
    assert walk["recommended_pixel_size"] == pytest.approx(1.8 / walk["reference_subject_height_px"])
    assert all((output / "godot" / frame).resolve().is_file() for frame in walk["frames"])
    bundle = read_json(output / "godot" / "sprite3d-bundle.json")
    assert bundle["default_action"] == "walk" and sorted(bundle["actions"]) == ["idle", "walk"]


@pytest.mark.resvg
def test_hero_review_sheets_and_travel_strip(hero_pixel):
    output, _ = hero_pixel
    for name in ("walk.png", "walk-onion.png", "walk-travel.png", "idle.png", "idle-onion.png"):
        assert (output / "review" / name).is_file(), name
    assert not (output / "review" / "idle-travel.png").exists()  # the idle has no stride


@pytest.mark.resvg
def test_same_inputs_give_identical_bytes(tmp_path):
    require_resvg()
    outputs = [tmp_path / "a", tmp_path / "b"]
    for output in outputs:
        assert run_rig(HERO_ANIM, output, "--clips", "idle").returncode == 0
    files = sorted(path.relative_to(outputs[0]) for path in outputs[0].rglob("*") if path.is_file())
    assert files and files == sorted(path.relative_to(outputs[1]) for path in outputs[1].rglob("*") if path.is_file())
    for relative in files:
        assert (outputs[0] / relative).read_bytes() == (outputs[1] / relative).read_bytes(), relative


@pytest.mark.resvg
def test_pixel_route_refuses_gradients_and_translucency(tmp_path):
    require_resvg()
    gradient = LEG_RIG.replace("</style>", '</style><defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/>'
                                           '</linearGradient></defs>').replace('class="c-body c-out"',
                                                                               'class="c-out" fill="url(#g)"')
    result = run_rig(write_case(tmp_path / "a", gradient), tmp_path / "out-a")
    assert result.returncode == 1 and "needs an opaque hex colour" in result.stderr
    faded = LEG_RIG.replace('class="c-body c-out"', 'class="c-body c-out" opacity="0.5"')
    result = run_rig(write_case(tmp_path / "b", faded), tmp_path / "out-b")
    assert result.returncode == 1 and "forbids opacity below 1" in result.stderr
    assert run_rig(write_case(tmp_path / "c", gradient), tmp_path / "out-c", "--route", "vector").returncode == 0


@pytest.mark.resvg
def test_outline_none_draws_no_lines_and_stays_on_palette(tmp_path):
    """Without an outline colour the pixel route draws neither the outline nor inner lines (both need a
    line colour), so every pixel is a slot fill."""
    require_resvg()
    output = tmp_path / "out"
    assert run_rig(write_case(tmp_path / "case"), output, "--outline", "none").returncode == 0
    report = read_json(output / "rig-report.json")
    assert report["palette"] == ["#3b5dc9", "#4a5a85", "#5a3a2a"]
    for frame in report["clips"]["stand"]["frames"]:
        assert frame["qa"]["off_palette"] == 0 and frame["qa"]["outline_gaps"] is None
        assert frame["ground"]["gap_px"] == 0  # no outline ring: the fill itself touches the ground


@pytest.mark.resvg
def test_clips_schema_v2_and_palette_gate(tmp_path):
    require_resvg()
    anim = write_case(tmp_path / "case")
    output = tmp_path / "out"
    assert run_rig(anim, output, "--clips-schema", "v2").returncode == 0
    clips = read_json(output / "clips.json")
    assert clips["schema"] == "generate2dsprite.animation_clips.v2"
    assert_valid_contract(clips, "sprite", "clips_input", skill="codeart2d")
    palette = tmp_path / "palette.json"
    palette.write_text(json.dumps({"body": "#3b5dc9", "leg": "#4a5a85", "outline": "#1a1c2c"}), encoding="utf-8")
    result = run_rig(anim, tmp_path / "out2", "--palette", palette)
    assert result.returncode == 1 and "colour #5a3a2a is not in the --palette" in result.stderr


@pytest.mark.resvg
def test_palette_variant_recolours_the_rig(tmp_path):
    require_resvg()
    palette = tmp_path / "palette.json"
    palette.write_text(json.dumps({"colors": {"body": "#3b5dc9", "leg": "#4a5a85", "boot": "#5a3a2a",
                                              "outline": "#1a1c2c"},
                                   "variants": {"night": {"boot": "#29366f", "body": "#5d275d"}}}), encoding="utf-8")
    output = tmp_path / "out"
    result = run_rig(write_case(tmp_path / "case"), output, "--palette", palette, "--variant", "night")
    assert result.returncode == 0, result.stderr
    report = read_json(output / "rig-report.json")
    assert report["palette"] == ["#1a1c2c", "#29366f", "#4a5a85", "#5d275d"]  # the base colours with the variant
    with Image.open(output / report["clips"]["stand"]["frames"][0]["file"]) as png:
        colours = {"#%02x%02x%02x" % tuple(rgb) for rgb in np.asarray(png.convert("RGBA")).reshape(-1, 4)[:, :3]}
    assert {"#5d275d", "#29366f"} <= colours and not {"#3b5dc9", "#5a3a2a"} & colours
    meta = read_json(output / "codeart-meta.json")
    assert {ref["path"].rsplit("/", 1)[-1] for ref in meta["qa"]["inputs"]} == {"leg.rig.svg", "leg.anim.json",
                                                                              "palette.json"}
