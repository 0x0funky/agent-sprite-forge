"""build_animation_clips.py: the improved fork's 13 regression tests (B02-T1) plus clips v2 and reviews.

AnimationClipTests is the fork's suite, ported unchanged except that publication now goes
through forge_core.staged_output, so the competing-publish test patches
forge_core.publish_directory_no_replace. The other classes cover v1 compatibility (golden
values recorded from the cfed170 builder), the v2 manifest fields, timing on the 60 Hz tick
grid, the lints, the review sheets and the CLI conventions. Contracts are checked against the
vendored generate2dsprite schemas, which hold the optional-field documentation requested in
handoff/B02-frames-and-clips.md section 5 (with the D12/D13 position and transition-hint semantics).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from forge_testutils import (FIXTURES_DIR, REPO_ROOT, SKILLS_DIR, assert_cli_help, assert_valid_contract,
                             contract_validator, load_script, run_cli, script_path)

MODULE = load_script("generate2dsprite", "build_animation_clips")
SCRIPT = script_path("generate2dsprite", "build_animation_clips")
V2 = "generate2dsprite.animation_clips.v2"

_INT0 = {"type": "integer", "minimum": 0}
_POSITIVE = {"type": "number", "exclusiveMinimum": 0}
# Optional fields the v2 builder writes, requested for sprite.schema.json in handoff section 5
# (JSON pointer, properties merged in): the record the handoff test compares. Integration applied them;
# test_requests_are_integrated checks that every requested property is in the vendored schema.
CLIPS_SCHEMA_PATCH = [
    ("/$defs/builtClip/properties", {
        "authored_frames": {"type": "array", "minItems": 1, "items": _INT0},
        "authored_duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
        "timing_source": {"enum": ["duration_ms", "ticks"]},
        "ticks": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 1}},
        "tick_hz": {"type": "integer", "minimum": 1},
        "fps_rational": {"type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"},
        "stride_world_units": _POSITIVE,
        "stride_source": {"const": "user_declared"},
        "nominal_travel_speed_world_units_per_second": _POSITIVE,
        "stride_px_per_frame": _POSITIVE,
        "nominal_speed_px_per_second": _POSITIVE,
        "cadence_ms": _POSITIVE,
        "speed_ref": _POSITIVE,
        "speed_ref_playback_rate": _POSITIVE,
        "keys": {"$ref": "#/$defs/clipSpec/properties/keys"},
        "keys_ms": {"type": "object", "additionalProperties": _INT0},
        "entry_frame": _INT0,
        "entry_ms": _INT0,
        "hitstop_ticks": _INT0,
        "hitstop_ms": _INT0,
        "role": {"enum": ["player", "enemy", "npc", "fx", "prop"]},
        "transition_hints": {"type": "array", "items": {
            "type": "object", "required": ["to", "entry_frame", "dissolve_ms", "mode"],
            "properties": {"to": {"type": "string", "minLength": 1}, "entry_frame": _INT0, "entry_ms": _INT0,
                           "dissolve_ms": _INT0, "dissolve_ticks": _INT0,
                           "mode": {"enum": ["dither", "premultiplied"]}}}},
        "holds": {"type": "array", "items": {
            "type": "object", "required": ["positions", "frames", "kind", "duration_ms"],
            "properties": {"positions": {"type": "array", "minItems": 2, "items": _INT0},
                           "frames": {"type": "array", "minItems": 2, "items": _INT0},
                           "kind": {"enum": ["repeat", "near_duplicate"]},
                           "max_distance": {"type": "number", "minimum": 0},
                           "duration_ms": {"type": "integer", "minimum": 1}}}},
        "seam": {"allOf": [{"$ref": "common.schema.json#/$defs/seamReport"},
                           {"type": "object", "required": ["adjacent_mean", "wrap_ratio"],
                            "properties": {"adjacent_mean": {"type": "number", "minimum": 0},
                                           "wrap_ratio": {"type": "number", "minimum": 0}}}]},
        "telegraph": {"type": "array", "items": {
            "type": "object", "required": ["tell_ms", "hit_ms", "ticks_60hz", "source"],
            "properties": {"tell_ms": _INT0, "hit_ms": _INT0, "ticks_60hz": {"type": "number", "minimum": 0},
                           "source": {"type": "string"}}}},
    }),
    ("/$defs/builtClip/properties/events_ms/items/properties", {"position": _INT0, "at_tick": _INT0}),
    ("/$defs/builtClip/properties/tick_grid/properties", {
        "frame_ticks": {"type": "array", "items": _INT0},
        "zero_tick_positions": {"type": "array", "items": _INT0},
        "even": {"type": "boolean"},
        "cycle_ticks": {"type": "number", "minimum": 0},
        "cycle_aligned": {"type": "boolean"},
        "method": {"type": "string"},
    }),
    ("/$defs/animation_clips_v2/properties", {
        "sampling": {"$ref": "common.schema.json#/$defs/sampling"},
        "pixel_art": {"type": "boolean"},
        "palette_ref": {"$ref": "common.schema.json#/$defs/relPath"},
        "palette_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
        "body_height_px": _POSITIVE,
        "shadow": {"$ref": "common.schema.json#/$defs/shadow"},
        "review": {"anyOf": [{"type": "null"}, {
            "type": "object", "required": ["scale", "backgrounds", "turn_test"],
            "properties": {"scale": {"type": "integer", "minimum": 1},
                           "backgrounds": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                           "turn_test": {"type": "object", "required": ["file", "cells"]}}}]},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
    }),
    ("/$defs/animation_clips_v2/properties/frames/items/properties", {
        "source": {"type": "object", "required": ["file_sha256"],
                   "properties": {"path": {"type": "string", "minLength": 1},
                                  "file_sha256": {"$ref": "common.schema.json#/$defs/sha256"},
                                  "bytes": _INT0, "size": {"$ref": "common.schema.json#/$defs/size2"},
                                  "mode": {"const": "RGBA"}}},
        "transparent_pixel_count": _INT0,
        "visible_pixel_count": _INT0,
    }),
]


def patched_validator(name: str):
    """Validator for the vendored generate2dsprite sprite/<name>, which holds CLIPS_SCHEMA_PATCH."""
    return contract_validator("sprite", name, skill="generate2dsprite")


def assert_clips_contract(test: unittest.TestCase, document: object, name: str = "animation_clips_v2") -> None:
    """Valid against the vendored contract, which holds the B02 documentation."""
    errors = [f"{error.json_path}: {error.message}" for error in patched_validator(name).iter_errors(document)]
    test.assertEqual(errors, [], f"sprite/{name}")


def test_requests_are_integrated() -> None:
    """Every property CLIPS_SCHEMA_PATCH requested is in the vendored sprite schema."""
    sprite = json.loads((SKILLS_DIR / "generate2dsprite" / "references" / "schemas" / "sprite.schema.json")
                        .read_text(encoding="utf-8"))
    for pointer, fragment in CLIPS_SCHEMA_PATCH:
        target = sprite
        for token in pointer.strip("/").split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
        assert set(fragment) <= set(target), pointer


class AnimationClipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "bundle"
        self.manifest = self.root / "clips.json"

    def sprite(self, name: str, phase: int = 0, size: tuple[int, int] = (18, 22)) -> str:
        frame = Image.new("RGBA", size, (80, 90, 100, 0))
        frame.paste((50 + phase * 15, 100, 160, 128), (5, 4 + phase % 3, 12, 17 + phase % 3))
        frame.putpixel((8, 8), (255, 0, 255, 255))
        frame.save(self.root / name)
        return name

    def contract(self) -> dict:
        return {
            "schema": MODULE.SCHEMA,
            "frames": [self.sprite("run-1.png", 0), self.sprite("run-2.png", 1)],
            "anchor_px": [9, 20],
            "clips": {"run": {"frames": [0, 1], "duration_ms": 80, "loop": True}},
            "states": {"moving": "run"},
        }

    def build(self, contract: dict) -> dict:
        self.manifest.write_text(json.dumps(contract), encoding="utf-8")
        return MODULE.build(self.manifest, self.output)

    def assert_unpublished(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".bundle.stage-*")), [])

    def test_nine_cells_export_eight_run_frames_and_separate_idle(self) -> None:
        names = [self.sprite(f"pose-{index}.png", index) for index in range(9)]
        source_bytes = [(self.root / name).read_bytes() for name in names]
        contract = {
            "frames": names, "anchor_px": [9, 20],
            "clips": {
                "run": {"frames": list(range(8)), "duration_ms": 80, "loop": True, "stride_world_units": 1.28},
                "idle": {"frames": [8], "duration_ms": 400, "loop": True},
            }, "states": {"moving": "run", "idle": "idle"},
        }
        result = self.build(contract)
        self.assertEqual(result["frame_size"], [18, 22])
        self.assertEqual(result["anchor_px"], [9, 20])
        self.assertEqual(result["clips"]["run"]["frames"], list(range(8)))
        self.assertEqual(result["clips"]["idle"]["frames"], [8])
        self.assertEqual(result["clips"]["run"]["duration_ms"], [80] * 8)
        self.assertEqual(result["clips"]["run"]["nominal_fps"], 12.5)
        self.assertEqual(result["clips"]["run"]["nominal_travel_speed_world_units_per_second"], 2)
        self.assertEqual(result["clips"]["idle"]["preview"]["total_duration_ms"], 400)
        self.assertEqual(result["states"]["idle"], "idle")
        for index, original in enumerate(source_bytes):
            self.assertEqual((self.output / "frames" / f"frame-{index:02d}.png").read_bytes(), original)
            self.assertEqual((self.root / names[index]).read_bytes(), original)
        self.assertFalse(result["diagnostics"]["visual_approval"])
        self.assertFalse(result["processing"]["per_frame_alignment"])
        self.assertEqual((self.output / "source-manifest.json").read_bytes(), self.manifest.read_bytes())
        with Image.open(self.output / "contact-sheet.png") as contact:
            self.assertGreater(contact.width, 18 * 3)
        self.assertNotIn(".stage-", (self.output / "animation-clips.json").read_text())

    def test_named_variable_duration_single_play_and_duplicate_merging(self) -> None:
        contract = self.contract()
        contract["frames"] = [{"name": "contact", "file": "run-1.png"}, {"name": "air", "file": "run-2.png"}]
        contract["clips"] = {"jump": {"frames": ["contact", "contact", "air"],
                                               "duration_ms": [60, 100, 240], "loop": False}}
        contract["states"] = {"jumping": "jump"}
        result = self.build(contract)
        clip = result["clips"]["jump"]
        self.assertEqual(clip["frames"], [0, 0, 1])
        self.assertEqual(clip["duration_ms"], [60, 100, 240])
        self.assertIsNone(clip["nominal_fps"])
        self.assertEqual(clip["preview"]["total_duration_ms"], 400)
        self.assertEqual(clip["preview"]["native_loop_count"], 1)
        self.assertEqual([frame["duration_ms"] for frame in clip["preview"]["decoded_frames"]], [160, 240])
        self.assertFalse(clip["last_to_first"]["applies_to_playback"])

    def test_single_frame_single_play_preview_keeps_duration_and_alpha(self) -> None:
        contract = self.contract()
        contract["clips"]["run"] = {"frames": [0], "duration_ms": 137, "loop": False}
        result = self.build(contract)
        preview = result["clips"]["run"]["preview"]
        self.assertEqual(preview["decoded_frame_count"], 1)
        self.assertEqual(preview["total_duration_ms"], 137)
        self.assertEqual(preview["native_loop_count"], 1)
        with Image.open(self.output / preview.get("file", result["clips"]["run"]["preview"]["file"])) as decoded:
            decoded.load()
            self.assertEqual(decoded.info["loop"], 1)
            self.assertEqual(decoded.info["duration"], 137)
            with Image.open(self.root / "run-1.png") as original:
                self.assertTrue(MODULE.FRAME_UTILS.same_visible_pixels(np.asarray(decoded.convert("RGBA")), np.asarray(original)))

    def test_duplicate_diagnostics_ignore_only_hidden_rgb(self) -> None:
        contract = self.contract()
        with Image.open(self.root / "run-1.png") as original:
            array = np.array(original)
        array[array[..., 3] == 0, :3] = (200, 210, 220)
        Image.fromarray(array).save(self.root / "run-2.png")
        result = self.build(contract)
        self.assertEqual(result["diagnostics"]["visible_pixel_duplicate_groups"], [[0, 1]])
        self.assertEqual(result["clips"]["run"]["last_to_first"]["changed_visible_pixels"], 0)
        self.assertNotEqual(result["frames"][0]["rgba_pixel_sha256"], result["frames"][1]["rgba_pixel_sha256"])

    def test_airborne_bottom_changes_do_not_change_common_anchor_or_canvas(self) -> None:
        contract = self.contract()
        contract["anchor_px"] = [9.5, 22]
        result = self.build(contract)
        self.assertEqual(result["anchor_px"], [9.5, 22])
        for record in result["frames"]:
            with Image.open(self.output / record["file"]) as frame:
                self.assertEqual(frame.size, (18, 22))
        self.assertEqual(result["frames"][0]["visible_pixel_count"], result["frames"][1]["visible_pixel_count"])

    def test_invalid_duration_reference_state_and_stride_reject_before_publication(self) -> None:
        cases = [
            ("duration_ms", 0, "durations"), ("duration_ms", [80], "one integer per frame"),
            ("duration_ms", [80, False], "durations"), ("frames", [0, 2], "out-of-range"),
            ("frames", ["missing"], "unknown frame"), ("frames", [True], "integer indices"),
            ("loop", "yes", "true or false"), ("stride_world_units", 0, "positive finite"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key, value=value):
                contract = self.contract()
                contract["clips"]["run"][key] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.build(contract)
                self.assert_unpublished()
        contract = self.contract()
        contract["states"] = {"idle": "missing"}
        with self.assertRaisesRegex(ValueError, "existing clip"):
            self.build(contract)
        self.assert_unpublished()

    def test_bad_shared_anchors_and_per_frame_anchor_are_rejected(self) -> None:
        for anchor in [[19, 20], [-1, 20], [9, 23], [True, 20], [9, float("nan")], [9]]:
            with self.subTest(anchor=anchor):
                contract = self.contract()
                contract["anchor_px"] = anchor
                with self.assertRaises(ValueError):
                    self.build(contract)
                self.assert_unpublished()
        contract = self.contract()
        contract["frames"][0] = {"name": "first", "file": "run-1.png", "anchor_px": [9, 19]}
        with self.assertRaisesRegex(ValueError, "Per-frame anchors"):
            self.build(contract)
        self.assert_unpublished()

    def test_mismatched_size_rejected(self) -> None:
        contract = self.contract()
        self.sprite("run-2.png", 1, (19, 22))
        with self.assertRaisesRegex(ValueError, "same native canvas"):
            self.build(contract)
        self.assert_unpublished()

    def test_opaque_and_empty_inputs_are_not_credible_sprite_alpha(self) -> None:
        for mode, color in [("RGB", (20, 40, 60)), ("RGBA", (20, 40, 60, 255)), ("RGBA", (20, 40, 60, 0))]:
            with self.subTest(mode=mode, color=color):
                contract = self.contract()
                Image.new(mode, (18, 22), color).save(self.root / "run-1.png")
                with self.assertRaisesRegex(ValueError, "transparent and visible"):
                    self.build(contract)
                self.assert_unpublished()

    def test_duplicate_names_missing_files_and_unknown_schema_are_rejected(self) -> None:
        contract = self.contract()
        contract["frames"] = ["run-1.png", "run-1.png"]
        with self.assertRaisesRegex(ValueError, "unique"):
            self.build(contract)
        self.assert_unpublished()
        contract = self.contract()
        contract["frames"][1] = "missing.png"
        with self.assertRaises(FileNotFoundError):
            self.build(contract)
        self.assert_unpublished()
        contract = self.contract()
        contract["schema"] = "unknown"
        with self.assertRaisesRegex(ValueError, "schema"):
            self.build(contract)
        self.assert_unpublished()

    def test_failure_late_in_preview_leaves_no_output(self) -> None:
        contract = self.contract()
        with mock.patch.object(MODULE, "make_contact_sheet", side_effect=ValueError("preview failure")):
            with self.assertRaisesRegex(ValueError, "preview failure"):
                self.build(contract)
        self.assert_unpublished()

    def test_existing_and_competing_output_are_preserved(self) -> None:
        contract = self.contract()
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "Refusing existing"):
            self.build(contract)
        self.assertEqual(marker.read_bytes(), b"original")

    def test_competing_destination_at_publish_is_not_replaced(self) -> None:
        contract = self.contract()
        publish = MODULE.forge_core.publish_directory_no_replace

        def conflict(stage: Path, final: Path) -> None:
            final.mkdir()
            (final / "winner.txt").write_bytes(b"another run")
            publish(stage, final)

        with mock.patch.object(MODULE.forge_core, "publish_directory_no_replace", side_effect=conflict):
            with self.assertRaises(FileExistsError):
                self.build(contract)
        self.assertEqual((self.output / "winner.txt").read_bytes(), b"another run")
        self.assertEqual(list(self.root.glob(".bundle.stage-*")), [])


# --------------------------------------------------------------------------- shared fixtures

def walker(phase: int, size: tuple[int, int] = (24, 32), *, offset: int = 0) -> Image.Image:
    """A small deterministic code-art walker: torso, head and two legs that swap with ``phase``."""
    width, height = size
    pixels = np.zeros((height, width, 4), np.uint8)
    x = 8 + offset + phase % 2
    pixels[6:22, x:x + 8] = (60, 120, 220, 255)
    pixels[2:7, x + 1:x + 7] = (240, 200, 160, 255)
    near, far = [(-3, 3), (-1, 1), (1, -1), (3, -3)][phase % 4]
    pixels[22:30, x + 1 + near // 2:x + 3 + near // 2] = (40, 40, 60, 255)
    pixels[22:30, x + 5 + far // 2:x + 7 + far // 2] = (80, 80, 100, 255)
    return Image.fromarray(pixels)


class _Bundle(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "bundle"
        self.manifest = self.root / "clips.json"

    def frames(self, count: int = 4, **options: object) -> list[str]:
        names = []
        for phase in range(count):
            names.append(f"walk-{phase}.png")
            walker(phase, **options).save(self.root / names[-1])
        return names

    def build(self, contract: dict, **options: object) -> dict:
        self.manifest.write_text(json.dumps(contract), encoding="utf-8")
        return MODULE.build(self.manifest, self.output, **options)

    def v2(self, clips: dict, **extra: object) -> dict:
        return {"schema": V2, "frames": self.frames(), "anchor_px": [12, 30], "clips": clips, **extra}

    def assert_unpublished(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".bundle.stage-*")), [])

    def lint_codes(self, result: dict) -> list[str]:
        return [item["code"] for item in result["diagnostics"]["lint"]]


GOLDEN_V1 = {
    "schema": "generate2dsprite.animation_clips.v1",
    "frame_size": [20, 24],
    "anchor_px": [10, 21],
    "anchor_semantics": "shared canvas/root origin, often a ground reference; not per-frame visible bottom",
    "frames": [
        {"index": 0, "name": "a", "source": {"size": [20, 24], "mode": "RGBA"},
         "rgba_pixel_sha256": "e044730f301ef47115e0d682716b82fcd5e3b70659e01daf5e1d978c7388bf52",
         "transparent_pixel_count": 382, "visible_pixel_count": 98, "file": "frames/frame-00.png"},
        {"index": 1, "name": "b", "source": {"size": [20, 24], "mode": "RGBA"},
         "rgba_pixel_sha256": "543327dbfe30b8f20f585c730e7258156fd72480a5c9ef1660dc556e305e8047",
         "transparent_pixel_count": 389, "visible_pixel_count": 91, "file": "frames/frame-01.png"},
        {"index": 2, "name": "c", "source": {"size": [20, 24], "mode": "RGBA"},
         "rgba_pixel_sha256": "fd142365010ee602117f921c3943ec236cd751ce5605038f48c5000af8d21701",
         "transparent_pixel_count": 382, "visible_pixel_count": 98, "file": "frames/frame-02.png"},
        {"index": 3, "name": "d", "source": {"size": [20, 24], "mode": "RGBA"},
         "rgba_pixel_sha256": "efaf305368ea052a84c08632fc9e9e226534548e2554b96cd44f86ce19182e92",
         "transparent_pixel_count": 382, "visible_pixel_count": 98, "file": "frames/frame-03.png"},
    ],
    "clips": {
        "run": {
            "frames": [0, 1, 2], "duration_ms": [80, 100, 120], "loop": True, "total_duration_ms": 300,
            "nominal_fps": None, "average_fps": 10.0, "stride_world_units": 30, "stride_source": "user_declared",
            "nominal_travel_speed_world_units_per_second": 100.0,
            "preview": {"file": "clips/clip-00.webp",
                        "decoded_frames": [{"index": 0, "start_ms": 0, "duration_ms": 80, "clip_positions": [0]},
                                           {"index": 1, "start_ms": 80, "duration_ms": 100, "clip_positions": [1]},
                                           {"index": 2, "start_ms": 180, "duration_ms": 120, "clip_positions": [2]}],
                        "decoded_frame_count": 3, "total_duration_ms": 300, "native_loop_count": 0,
                        "timeline_verified": True, "alpha_and_visible_rgb_exact": True,
                        "hidden_rgb_guaranteed": False, "lossless": True},
            "transitions": [{"from_position": 0, "to_position": 1, "premultiplied_rgb_mae": 12.001226,
                             "alpha_mae": 18.71875, "changed_visible_pixels": 108},
                            {"from_position": 1, "to_position": 2, "premultiplied_rgb_mae": 12.07067,
                             "alpha_mae": 18.71875, "changed_visible_pixels": 108}],
            "last_to_first": {"applies_to_playback": True, "premultiplied_rgb_mae": 20.738562, "alpha_mae": 32.125,
                              "changed_visible_pixels": 126}},
        "idle": {
            "frames": [3], "duration_ms": [250], "loop": False, "total_duration_ms": 250, "nominal_fps": 4.0,
            "average_fps": 4.0,
            "preview": {"file": "clips/clip-01.webp",
                        "decoded_frames": [{"index": 0, "start_ms": 0, "duration_ms": 250, "clip_positions": [0]}],
                        "decoded_frame_count": 1, "total_duration_ms": 250, "native_loop_count": 1,
                        "timeline_verified": True, "alpha_and_visible_rgb_exact": True,
                        "hidden_rgb_guaranteed": False, "lossless": True},
            "transitions": [],
            "last_to_first": {"applies_to_playback": False, "premultiplied_rgb_mae": 0.0, "alpha_mae": 0.0,
                              "changed_visible_pixels": 0}},
    },
    "states": {"moving": "run", "idle": "idle"},
    "diagnostics": {"visible_pixel_duplicate_groups": [[0, 3]], "visual_approval": False,
                    "note": "Pixel differences and duplicates do not establish animation quality or physical root "
                            "stability."},
    "contact_sheet": {"file": "contact-sheet.png", "native_scale": True, "annotated_with_shared_root": True,
                      "not_a_runtime_atlas": True},
    "processing": {"source_pngs_byte_identical": True, "resized": False, "cropped": False,
                   "per_frame_alignment": False, "chroma_keyed": False},
    "alpha_validation": "Every input has fully transparent and visible pixels; edge quality still requires visual "
                        "review.",
}


class V1CompatibilityTests(_Bundle):
    """A v1 manifest keeps every cfed170 output value; v2 fields only add."""

    def v1_fixture(self) -> dict:
        def frame(phase: int, hidden: tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
            pixels = np.zeros((24, 20, 4), np.uint8)
            pixels[..., :3] = hidden
            pixels[6 + phase % 2:20, 6 + phase:13 + phase] = (40 + 30 * phase, 120, 200 - 20 * phase, 255)
            pixels[8:11, 8 + phase:10 + phase] = (250, 240, 30, 160)
            return Image.fromarray(pixels)

        for name, image in (("a", frame(0)), ("b", frame(1)), ("c", frame(2)), ("d", frame(0, hidden=(9, 9, 9)))):
            image.save(self.root / f"{name}.png")
        return {
            "schema": "generate2dsprite.animation_clips.v1",
            "frames": ["a.png", "b.png", {"name": "c", "file": "c.png"}, "d.png"],
            "anchor_px": [10, 21],
            "clips": {"run": {"frames": [0, 1, "c"], "duration_ms": [80, 100, 120], "loop": True,
                              "stride_world_units": 30},
                      "idle": {"frames": [3], "duration_ms": 250, "loop": False}},
            "states": {"moving": "run", "idle": "idle"},
        }

    def assert_matches(self, expected: object, actual: object, path: str = "$") -> None:
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            for key, value in expected.items():
                self.assertIn(key, actual, path)
                self.assert_matches(value, actual[key], f"{path}.{key}")
        elif isinstance(expected, list):
            self.assertEqual(len(expected), len(actual), path)
            for index, (left, right) in enumerate(zip(expected, actual)):
                self.assert_matches(left, right, f"{path}[{index}]")
        elif isinstance(expected, float):
            self.assertAlmostEqual(expected, actual, places=5, msg=path)
        else:
            self.assertEqual(expected, actual, path)

    def test_v1_manifest_unchanged_output(self) -> None:
        result = self.build(self.v1_fixture())
        self.assert_matches(GOLDEN_V1, result)
        written = json.loads((self.output / "animation-clips.json").read_text(encoding="utf-8"))
        self.assert_matches(GOLDEN_V1, written)
        self.assertEqual(written["schema"], "generate2dsprite.animation_clips.v1")
        for index, name in enumerate("abcd"):
            source = (self.root / f"{name}.png").read_bytes()
            self.assertEqual((self.output / "frames" / f"frame-{index:02d}.png").read_bytes(), source)
            record = written["frames"][index]["source"]
            self.assertEqual((record["file_sha256"], record["bytes"]), (hashlib.sha256(source).hexdigest(), len(source)))
            self.assertEqual(record["path"], f"../{name}.png")  # manifest-relative now, never absolute
        self.assertEqual(written["source_manifest"]["path"], "../clips.json")
        self.assertEqual(written["source_manifest"]["file_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        for clip in written["clips"].values():
            preview = self.output / clip["preview"]["file"]
            self.assertEqual(clip["preview"]["file_sha256"], hashlib.sha256(preview.read_bytes()).hexdigest())
        self.assertEqual(written["clips"]["run"]["events_ms"], [])
        self.assertEqual(written["clips"]["run"]["tick_grid"]["frame_ticks"], [5, 6, 7])
        assert_clips_contract(self, written)

    def test_v1_ignores_v2_fields_with_a_lint(self) -> None:
        contract = self.v1_fixture()
        contract["clips"]["run"]["events"] = [{"at": 9, "name": "nonsense"}]
        contract["pixel_art"] = "maybe"
        result = self.build(contract)
        self.assertEqual(result["clips"]["run"]["events_ms"], [])
        self.assertNotIn("pixel_art", result)
        self.assertEqual(self.lint_codes(result), ["v2_field_ignored", "v2_field_ignored"])

    @unittest.skipUnless((REPO_ROOT / "handoff" / "B02-frames-and-clips.md").is_file(),
                         "handoff/ is removed once integration applies the requests")
    def test_handoff_requests_match_the_validated_proposal(self) -> None:
        text = (REPO_ROOT / "handoff" / "B02-frames-and-clips.md").read_text(encoding="utf-8")
        section = text.split("## 5. Schema change requests", 1)[1].split("## 6.", 1)[0]
        blocks = [json.loads(block.split("```", 1)[0]) for block in section.split("```json\n")[1:]]
        self.assertEqual(blocks[2:], [fragment for _, fragment in CLIPS_SCHEMA_PATCH])  # requests C to G, in order
        for pointer, _ in CLIPS_SCHEMA_PATCH:
            self.assertIn(f"`{pointer}`", section)

    def test_contract_fixtures_still_validate_with_the_proposal(self) -> None:
        folder = FIXTURES_DIR / "contracts"
        for name, definition in (("sprite.animation_clips_v2.valid.json", "animation_clips_v2"),
                                 ("sprite.animation_clips_v2.legacy-v1.json", "animation_clips_v2"),
                                 ("sprite.clips_input.valid.json", "clips_input"),
                                 ("sprite.clips_input.legacy-v1.json", "clips_input")):
            with self.subTest(fixture=name):
                assert_clips_contract(self, json.loads((folder / name).read_text(encoding="utf-8")), definition)


class ClipsV2Tests(_Bundle):
    """Appendix B clips_input v2 fields and the animation_clips_v2 output."""

    def test_pingpong_expansion_no_duplicate_endpoints(self) -> None:
        result = self.build(self.v2({"idle": {"frames": [0, 1, 2, 3], "duration_ms": [100, 110, 120, 130],
                                              "loop_policy": "pingpong",
                                              "events": [{"at": 1, "name": "sfx"}, {"at": 3, "name": "custom:peak"}]},
                                     "pair": {"frames": [0, 1], "duration_ms": 90, "loop_policy": "pingpong"},
                                     "still": {"frames": [2], "duration_ms": 90, "loop_policy": "pingpong"}}))
        idle = result["clips"]["idle"]
        self.assertEqual(idle["frames"], [0, 1, 2, 3, 2, 1])
        self.assertEqual(idle["duration_ms"], [100, 110, 120, 130, 120, 110])
        self.assertEqual((idle["authored_frames"], idle["authored_duration_ms"]), ([0, 1, 2, 3], [100, 110, 120, 130]))
        self.assertEqual((idle["loop"], idle["loop_policy"], idle["total_duration_ms"]), (True, "pingpong", 690))
        self.assertNotEqual(idle["frames"][-1], idle["frames"][0])
        self.assertTrue(all(left != right for left, right in zip(idle["frames"], idle["frames"][1:])))
        self.assertEqual([(event["name"], event["at_ms"], event["position"]) for event in idle["events_ms"]],
                         [("sfx", 100, 1), ("custom:peak", 330, 3), ("sfx", 580, 5)])
        self.assertEqual([frame["clip_positions"] for frame in idle["preview"]["decoded_frames"]],
                         [[0], [1], [2], [3], [4], [5]])
        self.assertEqual(result["clips"]["pair"]["frames"], [0, 1])
        self.assertEqual(result["clips"]["still"]["frames"], [2])
        self.assertNotIn("authored_frames", result["clips"]["pair"])
        assert_clips_contract(self, result)

    def test_events_validated(self) -> None:
        result = self.build(self.v2({"attack": {"frames": [0, 1, 2, 3], "duration_ms": [80, 60, 120, 200],
                                                "loop": False,
                                                "events": [{"at": 2, "name": "hit", "data": {"damage": 2}},
                                                           {"at": 1, "name": "tell"}, {"at": 3, "name": "end"},
                                                           {"at": 2, "name": "custom:shake"}]}}))
        events = result["clips"]["attack"]["events_ms"]
        self.assertEqual([(event["name"], event["at_ms"], event["at_tick"]) for event in events],
                         [("tell", 80, 5), ("hit", 140, 8), ("custom:shake", 140, 8), ("end", 260, 16)])
        self.assertEqual(events[1]["data"], {"damage": 2})
        bad = [({"at": 4, "name": "hit"}, "at must be a clip position from 0 to 3"),
               ({"at": True, "name": "hit"}, "at must be a clip position"),
               ({"at": 1, "name": "smash"}, "unknown event name 'smash'"),
               ({"at": 1, "name": "custom:"}, "unknown event name"),
               ({"at": 1}, "unknown event name None"),
               ("hit", "must be an object")]
        for event, message in bad:
            with self.subTest(event=event):
                self.output = self.root / "rejected"
                with self.assertRaisesRegex(ValueError, message):
                    self.build(self.v2({"attack": {"frames": [0, 1, 2, 3], "duration_ms": 80, "loop": False,
                                                   "events": [event]}}))
                self.assert_unpublished()
        with self.assertRaisesRegex(ValueError, "events must be a list"):
            self.build(self.v2({"attack": {"frames": [0], "duration_ms": 80, "loop": False, "events": {}}}))

    def test_tick_grid_report(self) -> None:
        result = self.build(self.v2({"run": {"frames": [0, 1, 2, 3], "duration_ms": 80, "loop": True},
                                     "walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop": True},
                                     "twitch": {"frames": [0, 1], "duration_ms": [7, 200], "loop": False}}))
        run = result["clips"]["run"]["tick_grid"]
        self.assertEqual((run["hz"], run["frame_ticks"], run["max_drift_ms"], run["even"]), (60, [5, 5, 4, 5], 6.667, False))
        self.assertEqual((run["cycle_ticks"], run["cycle_aligned"]), (19.2, False))
        walk = result["clips"]["walk"]["tick_grid"]
        self.assertEqual((walk["frame_ticks"], walk["max_drift_ms"], walk["even"], walk["cycle_aligned"]),
                         ([6, 6, 6, 6], 0.0, True, True))
        self.assertEqual(result["clips"]["twitch"]["tick_grid"]["zero_tick_positions"], [0])
        lint = {(item["code"], item.get("clip")) for item in result["diagnostics"]["lint"]}
        self.assertEqual(lint, {("uneven_ticks", "run"), ("vanishing_frames", "twitch")})
        checks = {check["id"]: check for check in result["qa"]["checks"]}
        self.assertEqual((checks["tick_grid"]["status"], checks["tick_grid"]["value"]), ("warn", 7.0))  # twitch: 7 ms
        self.output = self.root / "thirty"
        thirty = self.build(self.v2({"run": {"frames": [0, 1], "duration_ms": 80, "loop": True}}), tick_hz=30)
        self.assertEqual(thirty["clips"]["run"]["tick_grid"]["frame_ticks"], [2, 3])
        assert_clips_contract(self, result)

    def test_ticks_become_drift_free_integer_ms(self) -> None:
        self.assertEqual(MODULE.ticks_to_ms([5, 5, 5], 60), [83, 84, 83])
        self.assertEqual(MODULE.ticks_to_ms([1] * 7, 60), [17, 16, 17, 17, 16, 17, 17])
        self.assertEqual(sum(MODULE.ticks_to_ms([1] * 60, 60)), 1000)
        result = self.build(self.v2({"idle": {"frames": [0, 1, 2], "ticks": [5, 7, 5], "loop_policy": "pingpong"},
                                     "slow": {"frames": [0, 1], "ticks": 3, "tick_hz": 30, "loop": True}}))
        idle = result["clips"]["idle"]
        self.assertEqual(idle["frames"], [0, 1, 2, 1])
        self.assertEqual(idle["duration_ms"], [83, 117, 83, 117])
        self.assertEqual((idle["timing_source"], idle["ticks"], idle["tick_hz"]), ("ticks", [5, 7, 5], 60))
        self.assertEqual(idle["tick_grid"]["max_drift_ms"], 0.333)
        slow = result["clips"]["slow"]
        self.assertEqual((slow["duration_ms"], slow["nominal_fps"], slow["fps_rational"]), ([100, 100], 10.0, "10/1"))
        self.output = self.root / "both"
        with self.assertRaisesRegex(ValueError, "needs duration_ms or ticks, not both"):
            self.build(self.v2({"idle": {"frames": [0], "ticks": 5, "duration_ms": 80, "loop": True}}))
        with self.assertRaisesRegex(ValueError, "ticks must be a positive integer"):
            self.build(self.v2({"idle": {"frames": [0, 1], "ticks": [5], "loop": True}}))
        self.assert_unpublished()

    def test_v2_output_validates_as_animation_clips_v2(self) -> None:
        (self.root / "palette.json").write_text('{"colors": []}', encoding="utf-8")
        contract = self.v2({
            "walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop_policy": "cycle",
                     "events": [{"at": 0, "name": "step_l"}, {"at": 2, "name": "step_r"}], "entry_frame": 1,
                     "stride_world_units": 24, "stride_px_per_frame": 6, "cadence_ms": 200, "speed_ref": 60,
                     "transitions": [{"to": "idle", "entry_frame": 0, "dissolve_ms": 80, "mode": "dither"}],
                     "role": "player"},
            "idle": {"frames": ["walk-0"], "duration_ms": 400, "loop": True},
        }, sampling="nearest", pixel_art=True, palette_ref="palette.json", art_source="code", placeholder=False,
            body_height_px=28, shadow={"rx": 8, "ry": 3, "opacity": 0.35}, states={"moving": "walk", "idle": "idle"})
        assert_valid_contract(contract, "sprite", "clips_input", skill="generate2dsprite")
        result = self.build(contract)
        written = json.loads((self.output / "animation-clips.json").read_text(encoding="utf-8"))
        self.assertEqual(written, json.loads(json.dumps(result)))
        self.assertEqual(written["schema"], V2)
        self.assertEqual((written["art_source"], written["pixel_art"], written["palette_ref"]), ("code", True, "../palette.json"))
        self.assertEqual(written["palette_sha256"], hashlib.sha256(b'{"colors": []}').hexdigest())
        walk = written["clips"]["walk"]
        self.assertEqual((walk["entry_ms"], walk["nominal_speed_px_per_second"], walk["speed_ref_playback_rate"]),
                         (100, 60.0, 1.0))
        self.assertEqual(walk["transition_hints"], [{"to": "idle", "entry_frame": 0, "entry_ms": 0, "dissolve_ms": 80,
                                                     "dissolve_ticks": 5, "mode": "dither"}])
        assert_clips_contract(self, written)
        assert_valid_contract(written["qa"], "common", "qaEnvelope", skill="generate2dsprite")
        for reference in written["qa"]["inputs"] + written["qa"]["outputs"]:
            self.assertFalse(Path(reference["path"]).is_absolute(), reference)
        outputs = {reference["path"]: reference["sha256"] for reference in written["qa"]["outputs"]}
        self.assertIn("review/turn-test.png", outputs)
        self.assertEqual(outputs["frames/frame-00.png"],
                         hashlib.sha256((self.output / "frames/frame-00.png").read_bytes()).hexdigest())

    def test_keys_entry_hitstop_and_role_resolved(self) -> None:
        result = self.build(self.v2({"slash": {
            "frames": [0, 1, 2, 3], "duration_ms": [100, 50, 50, 300], "loop": False,
            "keys": {"wind_start": 0, "wind_peak": 1, "strike": 2, "recover": 3, "follow": 3},
            "events": [{"at": 2, "name": "impact"}], "hitstop_ticks": 4, "role": "player"}}))
        slash = result["clips"]["slash"]
        self.assertEqual(slash["keys"], {"wind_start": 0, "wind_peak": 1, "strike": 2, "recover": 3})
        self.assertEqual(slash["keys_ms"], {"wind_start": 0, "wind_peak": 100, "strike": 150, "recover": 200})
        self.assertEqual((slash["hitstop_ticks"], slash["hitstop_ms"], slash["role"]), (4, 67, "player"))
        self.assertEqual(self.lint_codes(result), ["unknown_key"])
        cases = [({"keys": {"strike": 1, "wind_peak": 2}}, "keys must not decrease"),
                 ({"entry_frame": 4}, "entry_frame must be a clip position from 0 to 3"),
                 ({"role": "boss"}, "role must be one of"),
                 ({"hitstop_ticks": -1}, "hitstop_ticks must be a whole number"),
                 ({"loop": True, "loop_policy": "oneshot"}, "contradicts loop_policy oneshot"),
                 ({"loop_policy": "bounce"}, "loop_policy must be cycle, pingpong or oneshot"),
                 ({"cadence_ms": 0}, "cadence_ms must be a positive finite number"),
                 ({"transitions": [{"to": "nowhere"}]}, "must name an existing clip"),
                 ({"transitions": [{"to": "slash", "mode": "wipe"}]}, "mode must be dither or premultiplied"),
                 ({"transitions": [{"to": "slash", "entry_frame": 9}]}, "entry_frame must be a position of slash")]
        for change, message in cases:
            with self.subTest(change=change):
                clip = {"frames": [0, 1, 2, 3], "duration_ms": 80, "loop": False, **change}
                if "loop_policy" in change and "loop" not in change:
                    clip.pop("loop")
                self.output = self.root / "rejected"
                with self.assertRaisesRegex(ValueError, message):
                    self.build(self.v2({"slash": clip}))
                self.assert_unpublished()

    def test_hitstop_without_hit_and_top_level_lints(self) -> None:
        result = self.build(self.v2({"slash": {"frames": [0, 1], "duration_ms": 100, "loop": False,
                                               "hitstop_ticks": 3}}, pixel_art=True, sampling="linear"))
        self.assertEqual(sorted(self.lint_codes(result)), ["hitstop_without_hit", "pixel_art_linear_sampling"])
        self.output = self.root / "rejected"
        for extra, message in (({"palette_ref": "missing.json"}, "palette_ref 'missing.json' was not found"),
                               ({"art_source": "ai"}, "art_source must be one of"),
                               ({"shadow": {"rx": -1, "ry": 2}}, "shadow needs non-negative rx and ry"),
                               ({"body_height_px": 0}, "body_height_px must be a positive finite number")):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.build(self.v2({"slash": {"frames": [0], "duration_ms": 100, "loop": False}}, **extra))
            self.assert_unpublished()

    def test_enemy_telegraph_under_28_ticks_warns(self) -> None:
        def slam(tell_ms: int, extra: dict | None = None) -> dict:
            clip = {"frames": [0, 1, 2], "duration_ms": [tell_ms, 100, 200], "loop": False, "role": "enemy",
                    "events": [{"at": 0, "name": "tell"}, {"at": 1, "name": "hit"}]}
            return {**clip, **(extra or {})}

        result = self.build(self.v2({"slam": slam(400), "jab": slam(470), "sweep": {
            "frames": [0, 1, 2], "duration_ms": [300, 100, 100], "loop": False, "role": "enemy",
            "keys": {"wind_start": 0, "strike": 1}}, "lunge": {
            "frames": [0, 1], "duration_ms": 100, "loop": False, "role": "enemy",
            "events": [{"at": 1, "name": "hit"}]}, "ally": {**slam(100), "role": "player"}}))
        clips = result["clips"]
        self.assertEqual(clips["slam"]["telegraph"], [{"tell_ms": 0, "hit_ms": 400, "source": "events", "ticks_60hz": 24.0}])
        self.assertEqual(clips["jab"]["telegraph"][0]["ticks_60hz"], 28.2)
        self.assertEqual(clips["sweep"]["telegraph"][0]["source"], "keys wind_start -> strike")
        self.assertNotIn("telegraph", clips["ally"])
        warnings = [(item["code"], item["clip"]) for item in result["diagnostics"]["lint"]]
        self.assertEqual(sorted(warnings), [("telegraph_missing", "lunge"), ("telegraph_short", "slam"),
                                            ("telegraph_short", "sweep")])
        short = next(item for item in result["diagnostics"]["lint"] if item["clip"] == "slam")
        self.assertEqual((short["value_ticks"], short["threshold_ticks"]), (24.0, 28))
        checks = {check["id"]: check for check in result["qa"]["checks"]}
        self.assertEqual((checks["telegraph"]["status"], checks["telegraph"]["value"]), ("warn", 18.0))

    def test_looping_enemy_tell_after_hit_wraps(self) -> None:
        result = self.build(self.v2({"spin": {"frames": [0, 1, 2], "duration_ms": [200, 200, 200], "loop": True,
                                              "role": "enemy", "events": [{"at": 0, "name": "hit"},
                                                                          {"at": 1, "name": "tell"}]}}))
        self.assertEqual(result["clips"]["spin"]["telegraph"],
                         [{"tell_ms": 200, "hit_ms": 600, "source": "events", "ticks_60hz": 24.0}])

    def test_manifest_with_a_utf8_bom_is_read(self) -> None:
        """Windows PowerShell 5.1 (Out-File -Encoding utf8) writes a BOM; cfed170 refused such manifests."""
        data = b"\xef\xbb\xbf" + json.dumps(self.v2({"walk": {"frames": [0, 1], "ticks": 6, "loop": True}})).encode()
        self.manifest.write_bytes(data)
        result = MODULE.build(self.manifest, self.output)
        self.assertEqual(result["clips"]["walk"]["duration_ms"], [100, 100])
        self.assertEqual((self.output / "source-manifest.json").read_bytes(), data)

    def test_code_art_frames_need_no_pipeline_meta(self) -> None:
        """B18/B19 feed code-art frames here directly; the folder holds PNGs and clips.json only."""
        contract = self.v2({"walk": {"frames": [0, 1, 2, 3], "ticks": 8, "loop": True}},
                           art_source="code", pixel_art=True, sampling="nearest")
        result = self.build(contract)
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.is_file()),
                         ["clips.json", "walk-0.png", "walk-1.png", "walk-2.png", "walk-3.png"])
        self.assertEqual(result["art_source"], "code")
        palette = Image.new("P", (24, 32))
        palette.putpalette([0, 0, 0, 200, 40, 40] + [0] * 762)
        palette.putpixel((5, 5), 1)
        palette.save(self.root / "walk-3.png", transparency=0)
        self.output = self.root / "indexed"
        with self.assertRaisesRegex(ValueError, r"indexed \(palette\) PNG.*Convert it first"):
            self.build(contract)
        self.assert_unpublished()


class ReviewTests(_Bundle):
    """Reviews: integer preview scale, onion skin, turn test, backgrounds, dissolves and holds."""

    def test_preview_scale_integer(self) -> None:
        contract = self.v2({"walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop": True}})
        result = self.build(contract, preview_scale=3)
        preview = result["clips"]["walk"]["preview"]
        self.assertEqual(preview["scale"], 3)
        sources = [np.asarray(walker(phase)) for phase in range(4)]
        with Image.open(self.output / preview["file"]) as decoded:
            self.assertEqual(decoded.size, (72, 96))
            for index in range(decoded.n_frames):
                decoded.seek(index)
                expected = np.repeat(np.repeat(sources[index], 3, axis=0), 3, axis=1)
                self.assertTrue(MODULE.FRAME_UTILS.same_visible_pixels(np.asarray(decoded.convert("RGBA")), expected))
        self.assertEqual((result["contact_sheet"]["scale"], result["contact_sheet"]["native_scale"]), (3, False))
        self.assertEqual(result["review"]["scale"], 3)
        for record in result["frames"]:
            self.assertEqual(Image.open(self.output / record["file"]).size, (24, 32))  # frames stay native
        for value in (0, 2.5, -1):
            with self.subTest(scale=value), self.assertRaisesRegex(ValueError, "preview-scale must be a positive integer"):
                self.output = self.root / "rejected"
                self.build(contract, preview_scale=value)
        with self.assertRaisesRegex(ValueError, "WebP allows at most 16383 px per side"):
            self.build(contract, preview_scale=700)
        with self.assertRaisesRegex(ValueError, "4800x6400 previews; scaled previews are for small pixel art"):
            self.build(contract, preview_scale=200)
        self.assert_unpublished()

    def test_sheet_scales_stay_within_their_pixel_budgets(self) -> None:
        self.assertEqual(MODULE.review_scale((24, 32), 3), 3)
        self.assertEqual(MODULE.review_scale((500, 500), 4), 2)   # 1000 x 1000 is the largest within 2 MP
        self.assertEqual(MODULE.review_scale((1600, 1600), 3), 1)
        self.assertEqual(MODULE.contact_sheet_scale((48, 48), 4, 1), 1)
        self.assertEqual(MODULE.contact_sheet_scale((960, 960), 145, 1), 1)  # native stays native, as in cfed170
        scale = MODULE.contact_sheet_scale((64, 64), 40, 16)
        self.assertTrue(1 < scale < 16)

        def area(value: int) -> int:
            return 4 * (max(64 * value, 180) + 16) * (10 * (64 * value + 48) + 30)

        self.assertLessEqual(area(scale), MODULE.MAX_REVIEW_PIXELS)
        self.assertGreater(area(scale + 1), MODULE.MAX_REVIEW_PIXELS)

    def test_turn_test_sheet_has_anchor_marks(self) -> None:
        names = []
        for index in range(2):
            pixels = np.zeros((24, 20, 4), np.uint8)
            pixels[4:24, 4:10] = (30, 160, 90, 255)  # the body stands left of the anchor x = 10
            pixels[8, 4 + index] = (250, 250, 250, 255)
            names.append(f"lean-{index}.png")
            Image.fromarray(pixels).save(self.root / names[-1])
        contract = {"schema": V2, "frames": names, "anchor_px": [10, 24],
                    "clips": {"idle": {"frames": [0, 1], "duration_ms": 100, "loop": True, "entry_frame": 1}}}
        result = self.build(contract, preview_scale=2)
        turn = result["review"]["turn_test"]
        cell = turn["cells"][0]
        self.assertEqual((cell["clip"], cell["frame"], cell["turn_slide_px"]), ("idle", 1, 6.0))
        with Image.open(self.output / turn["file"]) as image:
            sheet = np.asarray(image.convert("RGB"))
        mark = tuple(turn["anchor_mark_rgb"])
        x = cell["anchor_screen_x"]
        for top, bottom in cell["rows"]:
            dotted = [tuple(sheet[y, x]) for y in range(top, bottom, 2)]
            self.assertTrue(all(colour == mark for colour in dotted), "an anchor mark crosses each row")
        left = x - 20  # anchor 10 px at scale 2
        (top, _), (mirror_top, _) = cell["rows"]
        self.assertEqual(tuple(sheet[top + 20, left + 12]), (30, 160, 90))           # body at x 6 in the frame
        self.assertEqual(tuple(sheet[mirror_top + 20, left + 2 * 13 + 1]), (30, 160, 90))  # mirrored: x 13
        self.assertNotEqual(tuple(sheet[mirror_top + 20, left + 12]), (30, 160, 90))

    def test_font_fallback_pillow_10_0(self) -> None:
        original = MODULE.ImageFont.load_default

        def pillow_10_0(*args: object, **kwargs: object):
            if args or kwargs:
                raise TypeError("load_default() got an unexpected keyword argument 'size'")
            return original()

        def no_freetype(*args: object, **kwargs: object):
            if args or kwargs:
                raise ImportError("The _imagingft C module is not installed")
            return original()

        for name, stand_in in (("pillow-10.0", pillow_10_0), ("no-freetype", no_freetype)):
            with self.subTest(name), mock.patch.object(MODULE.ImageFont, "load_default", side_effect=stand_in):
                self.assertIsNotNone(MODULE.review_font(13))
                self.output = self.root / name
                result = self.build(self.v2({"walk": {"frames": [0, 1], "duration_ms": 100, "loop": True}}))
                self.assertTrue((self.output / "contact-sheet.png").is_file())
                self.assertTrue((self.output / result["review"]["filmstrips"]["walk"]["file"]).is_file())

    def test_near_duplicate_holds_reported(self) -> None:
        names = self.frames(3)
        twin = np.array(walker(0))
        twin[10, 10, 0] += 3  # compression-style noise: the same pose
        Image.fromarray(twin).save(self.root / "twin.png")
        contract = {"schema": V2, "frames": names + ["twin.png"], "anchor_px": [12, 30],
                    "clips": {"idle": {"frames": [0, 3, 1, 2, 2], "duration_ms": [100, 120, 80, 60, 60],
                                       "loop": True}}}
        result = self.build(contract)
        holds = result["clips"]["idle"]["holds"]
        self.assertEqual([(hold["positions"], hold["frames"], hold["kind"], hold["duration_ms"]) for hold in holds],
                         [([0, 1], [0, 3], "near_duplicate", 220), ([3, 4], [2, 2], "repeat", 120)])
        self.assertLessEqual(holds[0]["max_distance"], MODULE.NEAR_DUPLICATE_MAE)
        self.assertEqual(result["frames"][0]["near_duplicates"], [3])
        self.assertEqual(result["frames"][3]["near_duplicates"], [0])
        self.assertEqual(result["frames"][1]["near_duplicates"], [])
        self.assertEqual(result["frames"][2]["holds"], [{"clip": "idle", "positions": [3, 4], "kind": "repeat"}])
        self.assertEqual(result["diagnostics"]["near_duplicate_pairs"], [[0, 3, holds[0]["max_distance"]]])
        self.assertEqual(self.lint_codes(result), ["near_duplicate_hold"])
        checks = {check["id"]: check for check in result["qa"]["checks"]}
        self.assertEqual((checks["near_duplicate_holds"]["status"], checks["near_duplicate_holds"]["value"]), ("warn", 1))
        self.assertEqual(result["diagnostics"]["visible_pixel_duplicate_groups"], [])
        self.output = self.root / "stricter"
        stricter = self.build(contract, near_duplicate_mae=0.0)
        self.assertEqual([hold["kind"] for hold in stricter["clips"]["idle"]["holds"]], ["repeat"])
        assert_clips_contract(self, result)

    def test_transition_dissolve_preview(self) -> None:
        result = self.build(self.v2({
            "walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop": True,
                     "transitions": [{"to": "idle", "dissolve_ms": 100}, {"to": "idle", "dissolve_ms": 50,
                                                                          "mode": "dither"}]},
            "idle": {"frames": [1, 2], "duration_ms": 300, "loop": True, "entry_frame": 1}}))
        hints = result["clips"]["walk"]["transition_hints"]
        self.assertEqual([(hint["mode"], hint["entry_frame"], hint["entry_ms"], hint["dissolve_ticks"]) for hint in hints],
                         [("premultiplied", 1, 300, 6), ("dither", 1, 300, 3)])
        previews = result["review"]["transitions"]
        self.assertEqual([(item["from_frame"], item["to_frame"], len(item["weights"])) for item in previews],
                         [(3, 2, 7), (3, 2, 4)])
        for item in previews:
            self.assertTrue((self.output / item["file"]).is_file())
        first, second = np.asarray(walker(3)), np.asarray(walker(2))
        both = (first[..., 3] == 255) & (second[..., 3] == 255)
        for mode in ("premultiplied", "dither"):
            mixed = MODULE.dissolve(first, second, 0.5, mode)
            self.assertTrue((mixed[both, 3] == 255).all(), f"{mode}: no opacity dip where both poses are opaque")
        self.assertEqual(MODULE.dissolve(first, second, 0.0, "dither").tolist(), first.tolist())
        self.assertEqual(MODULE.dissolve(first, second, 1.0, "dither").tolist(), second.tolist())

    def test_preview_background_choice_and_no_reviews(self) -> None:
        contract = self.v2({"walk": {"frames": [0, 1], "duration_ms": 100, "loop": True}})
        result = self.build(contract, preview_background="#203040")
        self.assertEqual(result["review"]["filmstrips"]["walk"]["backgrounds"], ["#203040"])
        self.output = self.root / "dark"
        result = self.build(contract, preview_background="dark")
        self.assertEqual(result["review"]["backgrounds"], ["dark"])
        self.output = self.root / "plain"
        result = self.build(contract, reviews=False)
        self.assertIsNone(result["review"])
        self.assertFalse((self.output / "review").exists())
        self.output = self.root / "rejected"
        with self.assertRaisesRegex(ValueError, "preview-background must be"):
            self.build(contract, preview_background="beige")
        self.assert_unpublished()


class PreviewEncoderTests(_Bundle):
    """The libwebp 1.6 alpha-flag defect (see test_assemble_frames.WebpEncoderTests) in clip previews."""

    def opaque_block_contract(self) -> dict:
        names = []
        for index in range(2):
            pixels = np.zeros((24, 20, 4), np.uint8)
            pixels[4:24, 4:10] = (30, 160, 90, 255)
            pixels[8, 4 + index] = (250, 250, 250, 255)
            names.append(f"block-{index}.png")
            Image.fromarray(pixels).save(self.root / names[-1])
        return {"frames": names, "anchor_px": [10, 24],
                "clips": {"idle": {"frames": [0, 1], "duration_ms": 100, "loop": True}}}

    def test_opaque_block_frames_keep_alpha_in_previews(self) -> None:
        result = self.build(self.opaque_block_contract(), preview_scale=2)
        with Image.open(self.output / result["clips"]["idle"]["preview"]["file"]) as decoded:
            for index in range(decoded.n_frames):
                decoded.seek(index)
                with Image.open(self.root / f"block-{index}.png") as source:
                    expected = np.repeat(np.repeat(np.asarray(source)[..., 3], 2, axis=0), 2, axis=1)
                np.testing.assert_array_equal(np.asarray(decoded.convert("RGBA"))[..., 3], expected)

    def test_failed_preview_verification_is_reencoded_with_all_keyframes(self) -> None:
        real = MODULE.FRAME_UTILS.save_animation
        attempts = []

        def defective_first_attempt(path, images, duration, loop, *, all_keyframes=False):
            attempts.append(all_keyframes)
            if not all_keyframes:
                images = [image.convert("RGB") for image in images]
            real(path, images, duration, loop, all_keyframes=all_keyframes)

        with mock.patch.object(MODULE.FRAME_UTILS, "save_animation", side_effect=defective_first_attempt):
            result = self.build(self.opaque_block_contract())
        self.assertEqual(attempts, [False, True])
        self.assertTrue(result["clips"]["idle"]["preview"]["all_keyframes"])
        self.output = self.root / "always-defective"
        with mock.patch.object(MODULE.FRAME_UTILS, "save_animation", side_effect=lambda path, images, *rest, **options:
                               real(path, [image.convert("RGB") for image in images], *rest, **options)):
            with self.assertRaisesRegex(ValueError, "WebP changed alpha/visible RGB"):
                self.build(self.opaque_block_contract())
        self.assert_unpublished()


class DocumentationTests(_Bundle):
    """The manifests shown in references/frames-and-clips.md stay valid and build without warnings."""

    def documented_manifests(self) -> list[dict]:
        text = (SKILLS_DIR / "generate2dsprite" / "references" / "frames-and-clips.md").read_text(encoding="utf-8")
        blocks = [block for block in text.split("```json\n")[1:]]
        return [json.loads(block.split("```", 1)[0]) for block in blocks if '"anchor_px"' in block]

    def test_documented_manifests_validate_and_build_clean(self) -> None:
        manifests = self.documented_manifests()
        self.assertEqual([manifest["schema"] for manifest in manifests],
                         ["generate2dsprite.animation_clips.v1", V2])
        for manifest in manifests:
            with self.subTest(schema=manifest["schema"]):
                assert_valid_contract(manifest, "sprite", "clips_input", skill="generate2dsprite")
                folder = self.root / manifest["schema"]
                anchor_x, anchor_y = manifest["anchor_px"]
                canvas = (int(2 * anchor_x), int(anchor_y) + 10)  # the root sits on the canvas's centre line
                for index, entry in enumerate(manifest["frames"]):
                    path = folder / (entry if isinstance(entry, str) else entry["file"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    walker(index % 4, canvas, offset=index % 3).save(path)
                manifest_path = folder / "clips.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                result = MODULE.build(manifest_path, folder / "out")
                self.assertEqual(result["diagnostics"]["lint"], [])
                assert_clips_contract(self, result)


class CliTests(_Bundle):
    """Appendix D: cp1252 help, refusal of an existing output, nothing published on a QC failure."""

    def write(self, contract: dict) -> None:
        self.manifest.write_text(json.dumps(contract), encoding="utf-8")

    def test_help_works_under_cp1252_and_cp950(self) -> None:
        assert_cli_help("generate2dsprite", "build_animation_clips")

    def test_cli_refuses_existing_output(self) -> None:
        self.write(self.v2({"walk": {"frames": [0, 1], "ticks": 6, "loop": True}}))
        self.output.mkdir()
        result = run_cli([SCRIPT, "--manifest", self.manifest, "--output-dir", self.output], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: Refusing existing output directory"), result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cli_strict_failure_publishes_nothing(self) -> None:
        self.write(self.v2({"run": {"frames": [0, 1, 2, 3], "duration_ms": 80, "loop": True}}))
        result = run_cli([SCRIPT, "--manifest", self.manifest, "--output-dir", self.output, "--strict"], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: --strict: 1 QA warning(s): Clip run: 80 ms frames show for [4, 5] ticks", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assert_unpublished()

    def test_cli_success_prints_one_json_line_and_usage_errors_exit_1(self) -> None:
        self.write(self.v2({"walk": {"frames": [0, 1, 2, 3], "ticks": 6, "loop": True}}))
        result = run_cli([SCRIPT, "--manifest", self.manifest, "--output-dir", self.output, "--preview-scale", "2",
                          "--strict"], "cp1252")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        summary = json.loads(lines[0])
        self.assertEqual(Path(summary["output"]), self.output.resolve())
        self.assertEqual(Path(summary["metadata"]), self.output.resolve() / "animation-clips.json")
        self.assertEqual((summary["schema"], summary["clips"], summary["warnings"]), (V2, 1, 0))
        usage = run_cli([SCRIPT, "--manifest", self.manifest, "--output-dir", self.root / "x", "--preview-scale",
                         "2.5"], "cp1252")
        self.assertEqual(usage.returncode, 1)
        self.assertIn("error: argument --preview-scale", usage.stderr)


if __name__ == "__main__":
    unittest.main()
