"""Tests for scene_motion.py: registered masked loops, GOP-aligned encodes and decoded-file QA (B16-T2, B16-T3).

The synthetic scene is the hard case the hd2d seam findings describe: detailed texture drifting at sub-pixel
speed inside a water band. Encoded as one closed GOP at crf 18, the keyframe refresh pops at the wrap
(about 1.7 x the adjacent p95 here); more bits on both sides of the wrap bring it under 1.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, load_script, require_ffmpeg, run_cli,
                             script_path)


MOTION = load_script("generate2dmap", "scene_motion")
MASK = load_script("generate2dmap", "build_motion_mask")
AV = MOTION.forge_av
SCRIPT = script_path("generate2dmap", "scene_motion")
SKILL = "generate2dmap"

# The documents this module asks integration to add to map.schema.json (handoff section 5), applied in memory.
_FILE_REF = {"$ref": "common.schema.json#/$defs/fileRef"}
_SEAM = {"$ref": "common.schema.json#/$defs/seamReport"}
_FPS = {"$ref": "common.schema.json#/$defs/fpsValue"}
SCENE_MOTION_V1 = {
    "description": "scene_motion.py build: scene-motion.json, the record of a masked environment loop on a plate.",
    "type": "object",
    "required": ["schema", "plan", "plate", "clip", "registration", "loop", "composite", "video", "poster", "mask",
                 "encodeAttempts", "qa"],
    "properties": {
        "schema": {"const": "generate2dmap.scene_motion.v1"},
        "tool": {"$ref": "common.schema.json#/$defs/toolInfo"},
        "plan": {"allOf": [_FILE_REF], "properties": {"document": {"$ref": "#/$defs/motion_plan_v1"}}},
        "plate": {"allOf": [_FILE_REF], "required": ["size"],
                  "properties": {"size": {"$ref": "common.schema.json#/$defs/size2"}}},
        "clip": {"allOf": [_FILE_REF], "required": ["kind", "frames", "size", "fps"],
                 "properties": {"kind": {"enum": ["video", "frames"]}, "frames": {"type": "integer", "minimum": 1},
                                "size": {"$ref": "common.schema.json#/$defs/size2"}, "fps": _FPS}},
        "maskSource": _FILE_REF,
        "registration": {"type": "object", "required": ["fit", "scale", "offset", "footprint"],
                         "properties": {"fit": {"enum": ["contain", "cover", "explicit"]},
                                        "scale": {"type": "number", "exclusiveMinimum": 0},
                                        "offset": {"$ref": "common.schema.json#/$defs/point2"},
                                        "footprint": {"$ref": "common.schema.json#/$defs/box"},
                                        "edgeFadePx": {"type": "number", "minimum": 0},
                                        "coverageMin": {"type": "number", "minimum": 0, "maximum": 1},
                                        "shiftEstimates": {"type": "array"}}},
        "loop": {"type": "object", "required": ["policy", "overlap", "range", "frameCount", "fps", "reversedSteps",
                                                "frames"],
                 "properties": {
                     "policy": {"enum": ["forward-overlap", "pingpong"]},
                     "overlap": {"type": "integer", "minimum": 0},
                     "range": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2,
                               "maxItems": 2},
                     "frameCount": {"type": "integer", "minimum": 2}, "fps": _FPS,
                     "durationMs": {"type": "number", "exclusiveMinimum": 0},
                     "reversedSteps": {"type": "integer", "minimum": 0},
                     "frames": {"type": "array", "minItems": 2, "items": {
                         "type": "array", "minItems": 1, "maxItems": 2, "items": {
                             "type": "array", "prefixItems": [{"type": "integer", "minimum": 0},
                                                              {"type": "number", "minimum": 0, "maximum": 1}],
                             "minItems": 2, "maxItems": 2}}}}},
        "composite": {"type": "object", "required": ["outsideMaskMaxDelta", "sourceSeam"],
                      "properties": {"outsideMaskMaxDelta": {"const": 0}, "sourceSeam": _SEAM}},
        "video": {"allOf": [_FILE_REF],
                  "required": ["codec", "encodedSize", "crop", "crf", "keyint", "closedGop", "keyframes", "fps"],
                  "properties": {"codec": {"const": "h264"},
                                 "encodedSize": {"$ref": "common.schema.json#/$defs/size2"},
                                 "crop": {"$ref": "common.schema.json#/$defs/box"},
                                 "padding": {"$ref": "common.schema.json#/$defs/padding4"},
                                 "crf": {"type": "integer", "minimum": 1, "maximum": 51},
                                 "keyint": {"type": "integer", "minimum": 1}, "closedGop": {"const": True},
                                 "keyframes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                                 "wrapQp": {"type": ["integer", "null"], "minimum": 1, "maximum": 51},
                                 "wrapFrames": {"type": "integer", "minimum": 0}, "fps": _FPS,
                                 "decodedPixelsPerSecond": {"type": "number", "exclusiveMinimum": 0}}},
        "poster": _FILE_REF,
        "mask": _FILE_REF,
        "encodeAttempts": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["crf", "wrapQp", "bytes", "sha256", "decodedSeamOverP95", "status"],
            "properties": {"status": {"enum": ["pass", "fail"]},
                           "sha256": {"$ref": "common.schema.json#/$defs/sha256"}}}},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
        "metrics": {"type": "object"},
        "thresholds": {"type": "object"},
    },
}
SCENE_LOOP_QA_V1 = {
    "description": "scene_motion.py qa: loop-qa.json, a QA envelope of a decoded scene loop with its metrics.",
    "allOf": [{"$ref": "common.schema.json#/$defs/qaEnvelope"}],
    "type": "object",
    "required": ["schema", "metrics", "thresholds"],
    "properties": {
        "schema": {"const": "generate2dmap.scene_loop_qa.v1"},
        "metrics": {"type": "object", "required": ["frames", "keyframes", "decodedSeam", "meanOpacity",
                                                   "motionEnergy", "leakRing", "regions"],
                    "properties": {"decodedSeam": _SEAM}},
        "thresholds": {"type": "object"},
    },
}


def requested_errors(document, name):
    """Errors against the vendored generate2dmap schemas plus this module's requested $defs (in memory)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / SKILL / "references" / "schemas"
    schemas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.schema.json"))]
    map_schema = next(schema for schema in schemas if schema["$id"].endswith("/map.schema.json"))
    map_schema["$defs"].setdefault("scene_motion_v1", SCENE_MOTION_V1)
    map_schema["$defs"].setdefault("scene_loop_qa_v1", SCENE_LOOP_QA_V1)
    registry = Registry().with_resources((schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas)
    validator = Draft202012Validator({"$ref": f"{map_schema['$id']}#/$defs/{name}"}, registry=registry)
    return [f"{error.json_path}: {error.message}" for error in validator.iter_errors(document)]


def smoothstep(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def motion_main(*arguments):
    """scene_motion.main() in-process: (exit status, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = MOTION.main([str(argument) for argument in arguments])
    return status, stdout.getvalue(), stderr.getvalue()


def _noise(height, width, cell, seed):
    rng = np.random.default_rng(seed)
    small = rng.random((max(2, height // cell), max(2, width // cell))).astype(np.float32)
    return np.asarray(Image.fromarray(small).resize((width, height), Image.Resampling.BICUBIC), np.float32)


def drifting_scene(folder, *, size=(192, 112), frames=48, overlap=8, speed=0.3, boil=0.6, seed=3, motion="flow",
                   policy="forward-overlap"):
    """A painted plate and a clip whose water band drifts detailed texture at gusty sub-pixel speed.

    Writes plate.png, clip/frame_NNNNNN.png, plan.json and mask.png (the plan's mask) into ``folder``.
    """
    folder.mkdir(parents=True)
    width, height = size
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    plate = np.stack([90 + 60 * yy / height, 120 + 50 * yy / height, 190 - 60 * yy / height], axis=-1)
    plate += ((_noise(height, width, 2, 1) - 0.5) * 30 + (_noise(height, width, 9, 2) - 0.5) * 40)[..., None]
    plate = np.clip(plate, 0, 255)
    top, bottom = int(height * 0.5), int(height * 0.78)
    band = np.zeros((height, width), bool)
    band[top:bottom, 4:width - 4] = True
    texture = _noise(height, width * 4, 2, 11) * 0.6 + _noise(height, width * 4, 6, 12) * 0.4
    gust = 0.15 + 0.85 * np.clip(np.sin(np.arange(frames) / frames * 2 * np.pi * 1.7 + 0.5 + seed * 0.9), 0, 1) ** 2
    (folder / "clip").mkdir()
    position = 0.0
    for index in range(frames):
        position += speed * 2 * gust[index]
        whole, fraction = int(math.floor(position)), position - math.floor(position)
        rolled = np.roll(texture, -whole, axis=1)[:, :width + 1]
        moving = rolled[:, :width] * (1 - fraction) + rolled[:, 1:] * fraction
        frame = plate.copy()
        water = np.array([150, 170, 190], np.float32) + (moving[band][:, None] - 0.5) * 60
        frame[band] = plate[band] * 0.5 + water * 0.5
        frame += rng.normal(0, boil, frame.shape)
        Image.fromarray(np.clip(np.rint(frame), 0, 255).astype(np.uint8)).save(
            folder / "clip" / f"frame_{index:06d}.png")
    plate8 = np.rint(plate).astype(np.uint8)
    Image.fromarray(plate8).save(folder / "plate.png")
    loop = {"policy": policy, "range": [0, frames]}
    if policy == "forward-overlap":
        loop["overlap"] = overlap
    document = {
        "schema": "generate2dmap.motion_plan.v1", "sourceSize": [width, height],
        "regions": [{"id": "lake", "kind": "rect", "box": [4, top, width - 4, bottom], "feather": 4, "strength": 1,
                     "motion": motion}],
        "protected": [{"id": "rock", "box": [width // 2 - 8, top + 4, width // 2 + 8, top + 12], "margin": 2}],
        "loop": loop,
    }
    (folder / "plan.json").write_text(json.dumps(document), encoding="utf-8")
    plan = MASK.normalize_plan(document)
    mask = MASK.build_mask(plan, plate8).mask
    Image.fromarray(mask).save(folder / "mask.png")
    return folder


def build_args(scene, output, *extra):
    return ["build", "--plan", scene / "plan.json", "--plate", scene / "plate.png", "--clip", scene / "clip",
            "--fps", "24", "--mask", scene / "mask.png", "--output-dir", output, *extra]


class LoopAssemblyTests(unittest.TestCase):
    def test_forward_overlap_frame_map(self):
        frames = MOTION.loop_frames("forward-overlap", 10, 30, 4)
        self.assertEqual(len(frames), 16)  # (30 - 10) - 4
        self.assertEqual(frames[:12], [[(index, 1.0)] for index in range(14, 26)])
        for j, frame in enumerate(frames[12:]):
            weight = smoothstep((j + 1) / 5)
            self.assertEqual([source for source, _ in frame], [26 + j, 10 + j])
            self.assertAlmostEqual(frame[0][1], 1 - weight)
            self.assertAlmostEqual(frame[1][1], weight)
        self.assertEqual(MOTION.reversed_steps(frames), 0)  # water never plays backwards, wrap included

    def test_pingpong_frame_map_reverses(self):
        frames = MOTION.loop_frames("pingpong", 3, 9, 0)
        self.assertEqual([frame[0][0] for frame in frames], [3, 4, 5, 6, 7, 8, 7, 6, 5, 4])
        self.assertEqual(len(frames), 2 * 6 - 2)
        self.assertEqual(MOTION.reversed_steps(frames), 5)

    def test_plain_and_too_short_ranges(self):
        self.assertEqual(MOTION.loop_frames("forward-overlap", 0, 5, 0), [[(index, 1.0)] for index in range(5)])
        with self.assertRaisesRegex(ValueError, "at least 18 source frames"):
            MOTION.loop_frames("forward-overlap", 0, 17, 8)
        with self.assertRaises(ValueError):
            MOTION.loop_frames("pingpong", 4, 5, 0)

    def test_wrap_zones_cover_every_keyframe_and_the_frames_before_it(self):
        self.assertEqual(MOTION.wrap_zones(40, 40, 8, 10), "0,0,q=10/32,39,q=10")
        self.assertEqual(MOTION.wrap_zones(40, 20, 8, 10), "0,0,q=10/12,20,q=10/32,39,q=10")
        self.assertEqual(MOTION.wrap_zones(12, 4, 8, 6), "0,11,q=6")

    def test_encode_rungs(self):
        self.assertEqual(MOTION.encode_rungs(18, True), [{"crf": 18, "wrapQp": None}, {"crf": 18, "wrapQp": 10},
                                                         {"crf": 18, "wrapQp": 6}, {"crf": 18, "wrapQp": 4},
                                                         {"crf": 14, "wrapQp": 2}])
        self.assertEqual(MOTION.encode_rungs(18, False), [{"crf": 18, "wrapQp": None}])
        self.assertEqual(MOTION.encode_rungs(3, True), [{"crf": 3, "wrapQp": None}, {"crf": 3, "wrapQp": 1},
                                                        {"crf": 1, "wrapQp": 1}])  # duplicates collapse

    def test_composite_is_exact_and_leaves_the_plate_where_the_mask_is_zero(self):
        rng = np.random.default_rng(5)
        frame = rng.integers(0, 256, (20, 30, 3), dtype=np.uint8)
        plate = rng.integers(0, 256, (20, 30, 3), dtype=np.uint8)
        mask = rng.integers(0, 256, (20, 30), dtype=np.uint8)
        mask[:5] = 0
        mask[5:10] = 255
        out = MOTION.composite(frame, plate, mask)
        np.testing.assert_array_equal(out[:5], plate[:5])
        np.testing.assert_array_equal(out[5:10], frame[5:10])
        exact = np.floor((plate.astype(np.int64) * (255 - mask[..., None]) + frame.astype(np.int64) * mask[..., None])
                         / 255 + 0.5)
        np.testing.assert_array_equal(out, exact.astype(np.uint8))


@pytest.mark.ffmpeg
class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_ffmpeg()
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.scene = drifting_scene(cls.root / "scene")
        status, stdout, stderr = motion_main(*build_args(cls.scene, cls.root / "loop"))
        if status:
            raise AssertionError(stderr)
        cls.summary = json.loads(stdout)
        cls.report = json.loads((cls.root / "loop" / "scene-motion.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def test_composite_only_inside_the_mask(self):
        self.assertEqual(self.report["composite"]["outsideMaskMaxDelta"], 0)
        check = next(check for check in self.report["qa"]["checks"] if check["id"] == "outside_delta")
        self.assertEqual((check["status"], check["value"]), ("pass", 0))
        plate = np.asarray(Image.open(self.scene / "plate.png").convert("RGB"))
        poster = np.asarray(Image.open(self.root / "loop" / "poster.png").convert("RGB"))
        mask = np.asarray(Image.open(self.root / "loop" / "motion-mask.png"))
        np.testing.assert_array_equal(poster[mask == 0], plate[mask == 0])
        self.assertTrue((poster[mask == 255] != plate[mask == 255]).any())
        np.testing.assert_array_equal(mask, np.asarray(Image.open(self.scene / "mask.png")))  # full coverage
        self.assertEqual((self.report["poster"]["frame"], self.report["poster"]["from"]),
                         (0, "decoded loop frame 0 composited over the plate with the mask"))

    def test_frame_count_and_gop_match_the_plan(self):
        video = self.root / "loop" / "loop.mp4"
        self.assertEqual(self.summary["frames"], 40)  # 48 source frames - overlap 8
        self.assertEqual(AV.packet_count(video), 40)
        self.assertEqual(self.report["loop"]["frameCount"], 40)
        self.assertEqual(self.report["metrics"]["frames"], 40)  # decoded
        self.assertEqual((self.report["video"]["keyint"], self.report["video"]["keyframes"]), (40, [0]))
        self.assertEqual(self.report["video"]["fps"], "24/1")
        self.assertTrue(AV.moov_before_mdat(video) and AV.timestamps_increasing(video))

    def test_water_is_never_reversed(self):
        self.assertEqual(self.report["loop"]["reversedSteps"], 0)
        frames = self.report["loop"]["frames"]
        self.assertEqual(frames[0], [[8, 1.0]])
        self.assertEqual([source for source, _ in frames[-1]], [47, 7])

    def test_single_gop_encode_pops_and_more_bits_at_the_wrap_pass(self):
        attempts = self.report["encodeAttempts"]
        self.assertEqual((attempts[0]["wrapQp"], attempts[0]["status"]), (None, "fail"))
        self.assertGreater(attempts[0]["decodedSeamOverP95"], 1.0)
        self.assertEqual(attempts[-1]["status"], "pass")
        self.assertLessEqual(attempts[-1]["decodedSeamOverP95"], 1.0)
        self.assertEqual(self.report["video"]["wrapQp"], attempts[-1]["wrapQp"])
        self.assertEqual(self.report["video"]["sha256"], attempts[-1]["sha256"])
        self.assertEqual(self.summary["qa_status"], self.report["qa"]["status"])
        self.assertIn(self.report["qa"]["status"], ("pass", "warn"))

    def test_report_follows_the_contracts(self):
        assert_valid_contract(self.report["qa"], "common", "qaEnvelope", skill=SKILL)
        assert_valid_contract(self.report["composite"]["sourceSeam"], "common", "seamReport", skill=SKILL)
        assert_valid_contract(self.report["plan"]["document"], "map", "motion_plan_v1", skill=SKILL)
        self.assertEqual(requested_errors(self.report, "scene_motion_v1"), [])
        self.assertEqual(self.report["plate"]["path"], "../scene/plate.png")
        self.assertEqual(self.report["clip"]["kind"], "frames")
        names = sorted(path.name for path in (self.root / "loop").iterdir())
        self.assertEqual(names, ["loop.mp4", "motion-mask.png", "poster.png", "scene-motion.json"])

    def test_qa_of_the_published_loop_passes(self):
        output = self.root / "qa-pass"
        status, stdout, stderr = motion_main("qa", "--report", self.root / "loop" / "scene-motion.json",
                                             "--plate", self.scene / "plate.png", "--output-dir", output)
        self.assertEqual(status, 0, stderr)
        summary = json.loads(stdout)
        self.assertIn(summary["status"], ("pass", "warn"))
        document = json.loads((output / "loop-qa.json").read_text(encoding="utf-8"))
        checks = {check["id"]: check for check in document["checks"]}
        for ident in ("frame_count", "gop_aligned", "container", "decoded_seam", "poster_swap"):
            self.assertEqual(checks[ident]["status"], "pass", ident)
        # The poster is decoded frame 0 over the plate: swapping to the video changes no pixel.
        self.assertEqual(checks["poster_swap"]["value"], {"swapMAE": 0.0, "outsideMaskMaxDelta": 0})
        self.assertEqual(checks["leak_ring"]["status"], "pass")
        self.assertEqual(checks["protected_stability"]["status"], "pass")
        self.assertEqual(document["metrics"]["regions"][0]["mode"], "loop")
        assert_valid_contract(document, "common", "qaEnvelope", skill=SKILL)
        self.assertEqual(requested_errors(document, "scene_loop_qa_v1"), [])
        self.assertTrue((output / "seam-diff.png").is_file())

    def test_single_gop_crossfade_fails_decoded_qa(self):
        """hd2d seam-codec-findings: the crossfade loop shipped as one GOP at crf 18 pops at the wrap."""
        output = self.root / "single-gop"
        status, _stdout, stderr = motion_main(*build_args(self.scene, output, "--ladder", "off",
                                                          "--allow-seam-fail"))
        self.assertEqual(status, 0, stderr)
        self.assertIn("allow-seam-fail", stderr)
        report = json.loads((output / "scene-motion.json").read_text(encoding="utf-8"))
        self.assertEqual(report["qa"]["status"], "fail")
        self.assertEqual(report["video"]["keyframes"], [0])
        qa_dir = self.root / "single-gop-qa"
        status, stdout, _stderr = motion_main("qa", "--report", output / "scene-motion.json", "--plate",
                                              self.scene / "plate.png", "--output-dir", qa_dir)
        self.assertEqual(status, 1)
        summary = json.loads(stdout)
        self.assertEqual(summary["status"], "fail")
        self.assertGreater(summary["decoded_seam_over_p95"], 1.0)
        checks = {check["id"]: check["status"] for check in
                  json.loads((qa_dir / "loop-qa.json").read_text(encoding="utf-8"))["checks"]}
        self.assertEqual((checks["decoded_seam"], checks["gop_aligned"]), ("fail", "pass"))

    def test_keyint_must_divide_the_loop(self):
        status, _stdout, stderr = motion_main(*build_args(self.scene, self.root / "k7", "--keyint", "7"))
        self.assertEqual(status, 1)
        self.assertIn("does not divide the 40-frame loop", stderr)
        self.assertFalse((self.root / "k7").exists())
        output = self.root / "k20"
        status, _stdout, stderr = motion_main(*build_args(self.scene, output, "--keyint", "20", "--allow-seam-fail"))
        self.assertEqual(status, 0, stderr)
        report = json.loads((output / "scene-motion.json").read_text(encoding="utf-8"))
        self.assertEqual(report["video"]["keyframes"], [0, 20])
        gop = next(check for check in report["qa"]["checks"] if check["id"] == "gop_aligned")
        self.assertEqual(gop["status"], "pass")

    def test_mask_reaching_into_a_protected_core_is_refused(self):
        mask = np.asarray(Image.open(self.scene / "mask.png")).copy()
        mask[60:66, 90:100] = 200  # inside the rock core
        bad = self.root / "bad-mask.png"
        Image.fromarray(mask).save(bad)
        args = build_args(self.scene, self.root / "leak")
        args[args.index(self.scene / "mask.png")] = bad
        status, _stdout, stderr = motion_main(*args)
        self.assertEqual(status, 1)
        self.assertIn("protected cores", stderr)
        self.assertFalse((self.root / "leak").exists())


@pytest.mark.ffmpeg
class PolicyTests(unittest.TestCase):
    def setUp(self):
        require_ffmpeg()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_pingpong_is_refused_for_water_and_built_for_sway(self):
        water = drifting_scene(self.root / "water", frames=12, policy="pingpong")
        status, _stdout, stderr = motion_main(*build_args(water, self.root / "out"))
        self.assertEqual(status, 1)
        self.assertIn("must never reverse", stderr)
        self.assertFalse((self.root / "out").exists())
        cloth = drifting_scene(self.root / "cloth", frames=12, policy="pingpong", motion="sway")
        status, _stdout, stderr = motion_main(*build_args(cloth, self.root / "cloth-out", "--allow-seam-fail"))
        self.assertEqual(status, 0, stderr)
        report = json.loads((self.root / "cloth-out" / "scene-motion.json").read_text(encoding="utf-8"))
        self.assertEqual((report["loop"]["frameCount"], report["loop"]["reversedSteps"]), (22, 11))
        forward = next(check for check in report["qa"]["checks"] if check["id"] == "forward_only")
        self.assertEqual(forward["status"], "pass")  # every region is sway
        self.assertEqual(AV.packet_count(self.root / "cloth-out" / "loop.mp4"), 22)

    def test_a_loop_that_pops_before_encoding_is_refused(self):
        scene = drifting_scene(self.root / "jump", frames=24, speed=1.5)
        plan = json.loads((scene / "plan.json").read_text(encoding="utf-8"))
        plan["loop"] = {"policy": "forward-overlap", "overlap": 0, "range": [0, 24]}  # a hard cut at the wrap
        (scene / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        status, _stdout, stderr = motion_main(*build_args(scene, self.root / "out"))
        self.assertEqual(status, 1)
        self.assertIn("pops before encoding", stderr)
        self.assertFalse((self.root / "out").exists())

    def test_video_clip_of_another_size_is_registered_with_contain(self):
        scene = drifting_scene(self.root / "scene", frames=24, overlap=4)
        frames = [np.asarray(Image.open(path).resize((144, 84), Image.Resampling.LANCZOS))
                  for path in sorted((scene / "clip").glob("*.png"))]
        AV.encode_h264_loop(frames, scene / "provider.mp4", "24/1", crf=8)
        output = self.root / "out"
        status, stdout, stderr = motion_main("build", "--plan", scene / "plan.json", "--plate", scene / "plate.png",
                                             "--clip", scene / "provider.mp4", "--mask", scene / "mask.png",
                                             "--output-dir", output, "--allow-seam-fail")
        self.assertEqual(status, 0, stderr)
        report = json.loads((output / "scene-motion.json").read_text(encoding="utf-8"))
        registration = report["registration"]
        self.assertEqual((registration["fit"], registration["offset"]), ("contain", [0.0, 0.0]))
        self.assertAlmostEqual(registration["scale"], 192 / 144)
        self.assertEqual((report["clip"]["kind"], report["clip"]["frames"], report["clip"]["fps"]),
                         ("video", 24, "24/1"))
        self.assertEqual(json.loads(stdout)["frames"], 20)
        shifts = [item["shift"] for item in registration["shiftEstimates"] if item["status"] == "measured"]
        self.assertTrue(shifts and all(shift == [0, 0] for shift in shifts))

    def test_letterboxed_clip_fades_the_mask_at_its_edges(self):
        scene = drifting_scene(self.root / "scene", frames=24, overlap=4)
        cropped = self.root / "cropped"
        cropped.mkdir()
        for path in sorted((scene / "clip").glob("*.png")):  # the provider cut 16 px off each side
            Image.open(path).crop((16, 0, 176, 112)).save(cropped / path.name)
        output = self.root / "out"
        status, _stdout, stderr = motion_main("build", "--plan", scene / "plan.json", "--plate", scene / "plate.png",
                                              "--clip", cropped, "--fps", "24", "--mask", scene / "mask.png",
                                              "--output-dir", output, "--allow-seam-fail", "--edge-fade", "8")
        self.assertEqual(status, 0, stderr)
        report = json.loads((output / "scene-motion.json").read_text(encoding="utf-8"))
        self.assertEqual((report["registration"]["scale"], report["registration"]["offset"]), (1.0, [16.0, 0.0]))
        mask = np.asarray(Image.open(output / "motion-mask.png"))
        source = np.asarray(Image.open(scene / "mask.png"))
        row = 70
        self.assertTrue((mask[row, :16] == 0).all() and (mask[row, 176:] == 0).all())
        self.assertEqual(mask[row, 16], round(255 * smoothstep(0.5 / 8)))
        np.testing.assert_array_equal(mask[:, 24:168], source[:, 24:168])
        self.assertLess(report["registration"]["coverageMin"], 1.0)


@pytest.mark.ffmpeg
class DecodedQATests(unittest.TestCase):
    """qa on hand-made videos: invisible motion, motion outside the mask and a diluted mask."""

    def setUp(self):
        require_ffmpeg()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.size = (96, 64)
        rng = np.random.default_rng(1)
        self.plate = np.clip(rng.normal(128, 30, (64, 96, 3)), 0, 255).astype(np.uint8)
        Image.fromarray(self.plate).save(self.root / "plate.png")
        mask = np.zeros((64, 96), np.uint8)
        mask[20:44, 20:76] = 255
        self.mask = mask
        Image.fromarray(mask).save(self.root / "mask.png")

    def qa(self, frames, name, *extra, mask=None):
        video = self.root / f"{name}.mp4"
        AV.encode_h264_loop(frames, video, "24/1", crf=12)
        mask_path = self.root / "mask.png"
        if mask is not None:
            mask_path = self.root / f"{name}-mask.png"
            Image.fromarray(mask).save(mask_path)
        status, stdout, stderr = motion_main("qa", "--video", video, "--plate", self.root / "plate.png",
                                             "--mask", mask_path, "--output-dir", self.root / f"{name}-qa", *extra)
        document = json.loads((self.root / f"{name}-qa" / "loop-qa.json").read_text(encoding="utf-8"))
        return status, json.loads(stdout), {check["id"]: check for check in document["checks"]}, document

    def moving_frames(self, count=24, outside_noise=0.0):
        rng = np.random.default_rng(7)
        frames = []
        for index in range(count):
            frame = self.plate.astype(np.float32)
            phase = 2 * math.pi * index / count
            frame[20:44, 20:76] += 25 * np.sin(np.arange(56) / 4 + phase)[None, :, None]
            if outside_noise:
                frame[self.mask == 0] += rng.normal(0, outside_noise, (int((self.mask == 0).sum()), 3))
            frames.append(np.clip(np.rint(frame), 0, 255).astype(np.uint8))
        return frames

    def test_invisible_motion_warns_and_still_regions_are_judged_as_stills(self):
        plan = {"schema": "generate2dmap.motion_plan.v1", "sourceSize": list(self.size),
                "regions": [{"id": "pool", "kind": "rect", "box": [20, 20, 76, 44], "feather": 0, "strength": 1}],
                "loop": {"policy": "forward-overlap", "overlap": 0}}
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        _status, _summary, checks, document = self.qa([self.plate] * 12, "still", "--plan", self.root / "plan.json")
        self.assertEqual(checks["motion_energy"]["status"], "warn")
        self.assertEqual(document["metrics"]["regions"][0]["mode"], "still")
        self.assertEqual(checks["region_stability"]["value"]["stills"], ["pool"])

    def test_mean_alpha_below_seven_percent_warns(self):
        sparse = np.zeros((64, 96), np.uint8)
        sparse[28:36, 30:50] = 255  # 2.6% of the plate
        _status, _summary, checks, _document = self.qa(self.moving_frames(), "sparse", mask=sparse)
        self.assertEqual(checks["mean_opacity"]["status"], "warn")
        self.assertLess(checks["mean_opacity"]["value"], 0.07)
        _status, _summary, checks, _document = self.qa(self.moving_frames(), "wide")
        self.assertEqual(checks["mean_opacity"]["status"], "pass")
        self.assertEqual(checks["motion_energy"]["status"], "pass")

    def test_motion_outside_the_mask_shows_in_the_leak_ring(self):
        _status, _summary, clean, _document = self.qa(self.moving_frames(), "clean")
        self.assertEqual(clean["leak_ring"]["status"], "pass")
        _status, _summary, leaky, document = self.qa(self.moving_frames(outside_noise=4.0), "leaky")
        self.assertEqual(leaky["leak_ring"]["status"], "warn")
        self.assertGreater(document["metrics"]["leakRing"]["meanStep"], clean["leak_ring"]["value"])

    def test_poster_must_match_the_plate_outside_the_mask(self):
        poster = self.plate.copy()
        poster[0:4, 0:4] = 0
        Image.fromarray(poster).save(self.root / "poster.png")
        status, summary, checks, _document = self.qa(self.moving_frames(), "poster", "--poster",
                                                     self.root / "poster.png")
        self.assertEqual(checks["poster_swap"]["status"], "fail")
        self.assertEqual((status, summary["status"]), (1, "fail"))


class ReferenceDocTests(unittest.TestCase):
    """background-scenes.md (B16-T4): its example plan and single-line commands must stay valid."""

    reference = SKILLS_DIR / SKILL / "references" / "background-scenes.md"

    def blocks(self, language):
        text = self.reference.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        return re.findall(rf"```{language}\n(.*?)```", text, flags=re.S)

    def test_example_plan_is_a_valid_motion_plan(self):
        document = json.loads(self.blocks("json")[0])
        assert_valid_contract(document, "map", "motion_plan_v1", skill=SKILL)
        plan = MASK.normalize_plan(document)
        self.assertEqual([region["motion"] for region in plan["regions"]], ["flow", "sway", "flow"])
        self.assertEqual(len(MOTION.loop_frames(plan["loop"]["policy"], *plan["loop"]["range"],
                                                plan["loop"]["overlap"])), 120)

    def test_commands_are_single_lines_that_parse(self):
        parsers = {"build_motion_mask.py": MASK.build_parser(), "scene_motion.py": MOTION.build_parser(),
                   "assemble_frames.py": load_script("generate2dsprite", "assemble_frames").build_parser()}
        commands = [line for block in self.blocks("bash") for line in block.splitlines() if line.strip()]
        self.assertEqual(len(commands), 4)
        for command in commands:
            words = shlex.split(command)
            self.assertEqual(words[0], "python")
            script = Path(words[1]).name
            with self.subTest(script=script), contextlib.redirect_stderr(io.StringIO()):
                parsers[script].parse_args(words[2:])


@pytest.mark.ffmpeg
class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_help_is_ascii_under_legacy_consoles(self):
        assert_cli_help(SKILL, "scene_motion")
        for verb in ("build", "qa"):
            result = run_cli([SCRIPT, verb, "--help"], "cp1252", timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.isascii() and "--output-dir" in result.stdout)

    def test_refuses_an_existing_output_directory(self):
        require_ffmpeg()
        scene = drifting_scene(self.root / "scene", frames=20, overlap=4)
        existing = self.root / "out"
        existing.mkdir()
        result = run_cli([SCRIPT, *build_args(scene, existing)], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: "), result.stderr)
        self.assertEqual(list(existing.iterdir()), [])

    def test_failed_gate_publishes_nothing(self):
        require_ffmpeg()
        scene = drifting_scene(self.root / "scene")
        output = self.root / "out"
        result = run_cli([SCRIPT, *build_args(scene, output, "--ladder", "off")], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pops at the wrap", result.stderr)
        self.assertTrue(result.stderr.isascii())
        self.assertFalse(output.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["scene"])  # no stage left behind

    def test_strict_qa_failure_publishes_nothing(self):
        require_ffmpeg()
        scene = drifting_scene(self.root / "scene")
        status, _stdout, stderr = motion_main(*build_args(scene, self.root / "loop", "--ladder", "off",
                                                          "--allow-seam-fail"))
        self.assertEqual(status, 0, stderr)
        output = self.root / "qa"
        result = run_cli([SCRIPT, "qa", "--report", self.root / "loop" / "scene-motion.json", "--plate",
                          scene / "plate.png", "--output-dir", output, "--strict"], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("decoded_seam", result.stderr)
        self.assertFalse(output.exists())
        shutil.rmtree(self.root / "loop")


if __name__ == "__main__":
    unittest.main()
