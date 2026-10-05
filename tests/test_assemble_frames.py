"""assemble_frames.py: the improved fork's 16 regression tests (B02-T1) plus the B02 v2 features.

AssembleFramesTests is the fork's suite, ported unchanged except that publication now goes
through forge_core.staged_output, so the competing-publish test patches
forge_core.publish_directory_no_replace. The other classes cover lossless input expansion,
chroma keying, the unified crop-box schema, the cross-cell spill check, ownership slicing,
the loop tools, the CLI conventions and the full_frames_v2 contract (requested in
handoff/B02-frames-and-clips.md section 5, now in shared/schemas and validated against the vendored copy).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zlib

import numpy as np
from PIL import Image

from forge_testutils import (REPO_ROOT, SKILLS_DIR, assert_cli_help, contract_validator, load_script,
                             make_magenta_sheet, real_fixture, run_cli, script_path)

MODULE = load_script("generate2dsprite", "assemble_frames")
SCRIPT = script_path("generate2dsprite", "assemble_frames")

# Schema additions requested in handoff/B02-frames-and-clips.md section 5 (common.schema.json
# $defs.cropBoxes and sprite.schema.json $defs.full_frames_v2), kept as the record the handoff test
# compares. Integration merged cropBoxes with B10's request (id optional) and validation uses the vendored schemas.
_SHA = {"$ref": "common.schema.json#/$defs/sha256"}
_SIZE = {"$ref": "common.schema.json#/$defs/size2"}
_REL = {"$ref": "common.schema.json#/$defs/relPath"}
_COUNT = {"type": "integer", "minimum": 0}
PROPOSED_COMMON_DEFS = {
    "cropBoxes": {
        "description": "Crop boxes of a sheet, one form for every tool (DOC-18): items with an optional id and a "
                       "half-open [x0, y0, x1, y1) box in sheet pixels. Tools keep reading the legacy forms: a bare "
                       "list of boxes (assemble_frames --crop-boxes) and {props: [{label, source_box}]} "
                       "(extract_prop_pack --boxes-file).",
        "type": "object",
        "required": ["items"],
        "properties": {
            "schema": {"const": "forge-crop-boxes/v1"},
            "items": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["box"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "pattern": "\\S"},
                    "box": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 4, "maxItems": 4},
                }}},
        },
    },
}
PROPOSED_SPRITE_DEFS = {
    "full_frames_v2": {
        "description": "animation.json written by assemble_frames.py: complete frames packed without resizing or "
                       "alignment, an atlas, a decoded and verified lossless WebP, and loop diagnostics. "
                       "generate2dsprite.full_frames.v1 documents stay valid.",
        "type": "object",
        "required": ["schema", "frame_size", "source_frame_count", "sources", "frames", "sequence", "duration_ms",
                     "total_duration_ms", "atlas", "webp", "transitions"],
        "properties": {
            "schema": {"enum": ["generate2dsprite.full_frames.v1", "generate2dsprite.full_frames.v2"]},
            "frame_size": _SIZE,
            "source_frame_count": {"type": "integer", "minimum": 1},
            "sources": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["file_sha256", "size"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "file_sha256": _SHA, "bytes": _COUNT, "size": _SIZE,
                    "mode": {"enum": ["RGB", "RGBA"]},
                    "source_mode": {"type": "string"},
                    "bit_depth": {"enum": [1, 2, 4, 8, 16]},
                    "conversion": {"type": "string"},
                    "key": {"type": "object", "required": ["quality", "key_rgb"],
                            "properties": {"quality": {"type": "string"},
                                           "key_rgb": {"$ref": "common.schema.json#/$defs/rgb"},
                                           "qa": {"type": "object"}}},
                }}},
            "frames": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["index", "file", "size", "mode", "rgba_pixel_sha256"],
                "properties": {
                    "index": _COUNT, "file": _REL, "source_index": _COUNT,
                    "crop_box": {"$ref": "common.schema.json#/$defs/box"},
                    "crop_id": {"type": "string", "minLength": 1},
                    "sheet_origin": {"$ref": "common.schema.json#/$defs/point2"},
                    "derived": {"type": "object", "required": ["blend_of", "weight"],
                                "properties": {"blend_of": {"type": "array", "items": _COUNT, "minItems": 2,
                                                            "maxItems": 2},
                                               "weight": {"type": "number", "exclusiveMinimum": 0,
                                                          "exclusiveMaximum": 1},
                                               "curve": {"type": "string"}}},
                    "size": _SIZE, "mode": {"enum": ["RGB", "RGBA"]},
                    "file_sha256": _SHA, "rgba_pixel_sha256": _SHA,
                }}},
            "sequence": {"type": "array", "minItems": 1, "items": _COUNT},
            "duration_ms": {"type": "integer", "minimum": 1},
            "total_duration_ms": {"type": "integer", "minimum": 1},
            "loop": {"const": 0},
            "atlas": {"type": "object", "required": ["file", "size", "rows", "cols"],
                      "properties": {"file": _REL, "size": _SIZE, "rows": {"type": "integer", "minimum": 1},
                                     "cols": {"type": "integer", "minimum": 1}, "file_sha256": _SHA}},
            "webp": {"type": "object", "required": ["file", "total_duration_ms", "timeline_verified"],
                     "properties": {"file": _REL, "total_duration_ms": {"type": "integer", "minimum": 1},
                                    "timeline_verified": {"const": True}, "file_sha256": _SHA}},
            "transitions": {"type": "array", "items": {"type": "object", "required": ["from_frame", "to_frame", "wrap"]}},
            "loop_seam": {"anyOf": [{"type": "null"}, {"allOf": [
                {"$ref": "common.schema.json#/$defs/seamReport"},
                {"type": "object", "required": ["adjacent_mean", "wrap_ratio"],
                 "properties": {"adjacent_mean": {"type": "number", "minimum": 0},
                                "wrap_ratio": {"type": "number", "minimum": 0}}}]}]},
            "loop_overlap": {"anyOf": [{"type": "null"}, {
                "type": "object", "required": ["frames", "played_before", "played_after"],
                "properties": {"frames": {"type": "integer", "minimum": 2},
                               "played_before": {"type": "array", "items": _COUNT},
                               "played_after": {"type": "array", "items": _COUNT}}}]},
            "static_regions": {"type": "object", "additionalProperties": {
                "type": "object", "required": ["box", "reference_frame", "against_reference"],
                "properties": {"box": {"$ref": "common.schema.json#/$defs/box"}}}},
            "key": {"type": "object", "required": ["mode"], "properties": {"mode": {"enum": ["none", "chroma"]}}},
            "slicing": {"type": "object", "required": ["mode"],
                        "properties": {"mode": {"enum": ["inputs", "grid", "ownership"]},
                                       "padding": {"$ref": "common.schema.json#/$defs/padding4"},
                                       "canvas": _SIZE}},
            "spill_check": {"anyOf": [{"type": "null"}, {
                "type": "object", "required": ["status"],
                "properties": {"status": {"enum": ["clean", "resolved", "allowed", "skipped"]},
                               "crossing": {"type": "array", "items": {
                                   "type": "object", "required": ["owner_id", "pixels_over", "bbox"]}}}}]},
            "provenance": {"type": "object"},
            "validation": {"type": "object"},
            "processing": {"type": "object"},
            "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"},
        },
        "if": {"properties": {"schema": {"const": "generate2dsprite.full_frames.v2"}}, "required": ["schema"]},
        "then": {"required": ["loop_seam", "key", "slicing", "spill_check", "qa"]},
    },
}


def proposed_validator(domain: str, name: str):
    """Draft 2020-12 validator for the vendored generate2dsprite <domain>/<name> (the proposals are integrated)."""
    return contract_validator(domain, name, skill="generate2dsprite")


def assert_contract(test: unittest.TestCase, document: object, domain: str, name: str) -> None:
    errors = [f"{error.json_path}: {error.message}" for error in proposed_validator(domain, name).iter_errors(document)]
    test.assertEqual(errors, [], f"{domain}/{name} violations")


HANDOFF = REPO_ROOT / "handoff" / "B02-frames-and-clips.md"


def handoff_schema_blocks() -> list[object]:
    """The JSON fragments of the handoff's section 5, in order (A to G)."""
    section = HANDOFF.read_text(encoding="utf-8").split("## 5. Schema change requests", 1)[1].split("## 6.", 1)[0]
    return [json.loads(block.split("```", 1)[0]) for block in section.split("```json\n")[1:]]


class SchemaProposalTests(unittest.TestCase):
    @unittest.skipUnless(HANDOFF.is_file(), "handoff/ is removed once integration applies the requests")
    def test_handoff_requests_match_the_validated_proposal(self) -> None:
        blocks = handoff_schema_blocks()
        self.assertEqual(blocks[:2], [PROPOSED_COMMON_DEFS, PROPOSED_SPRITE_DEFS])

    def test_cfed170_full_frames_v1_document_stays_valid(self) -> None:
        legacy = {
            "schema": "generate2dsprite.full_frames.v1", "output_directory": "C:/work/output", "frame_size": [7, 5],
            "source_frame_count": 1, "sequence": [0], "duration_ms": 250, "total_duration_ms": 250, "loop": 0,
            "sources": [{"path": "C:/work/one.png", "file_sha256": "0" * 64, "bytes": 90, "size": [7, 5], "mode": "RGB"}],
            "frames": [{"index": 0, "file": "frames/frame-00.png", "source_index": 0, "size": [7, 5], "mode": "RGB",
                        "file_sha256": "1" * 64, "rgba_pixel_sha256": "2" * 64}],
            "atlas": {"file": "atlas.png", "size": [7, 5], "rows": 1, "cols": 1, "file_sha256": "3" * 64,
                      "rgba_pixels_exact": True},
            "webp": {"file": "animation.webp", "total_duration_ms": 250, "timeline_verified": True, "loop": 0},
            "transitions": [{"from_sequence_position": 0, "to_sequence_position": 0, "from_frame": 0, "to_frame": 0,
                             "wrap": True}],
            "static_regions": {}, "provenance": {"user_manifest": None, "user_assertions_verified": False},
            "validation": {"visual_approval": False}, "processing": {"resized": False},
        }
        assert_contract(self, legacy, "sprite", "full_frames_v2")


class AssembleFramesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"

    def png(self, name: str, image: Image.Image) -> Path:
        path = self.root / name
        image.save(path)
        return path

    def args(self, *arguments: str) -> object:
        return MODULE.build_parser().parse_args([*arguments, "--output-dir", str(self.output)])

    def assert_clean_failure(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".output.stage-*")), [])

    def test_rectangular_odd_dimensions_preserve_native_pixels_and_provenance(self) -> None:
        first = Image.new("RGB", (7, 5), (255, 0, 255))
        second = first.copy()
        second.putpixel((6, 4), (3, 70, 240))
        paths = [self.png("one.png", first), self.png("two.png", second)]
        originals = [path.read_bytes() for path in paths]
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"prompt": "A quiet harbor", "references": ["master.png"]}), encoding="utf-8")
        result = MODULE.assemble(self.args("--input", *map(str, paths), "--manifest", str(manifest)))
        self.assertEqual(result["frame_size"], [7, 5])
        self.assertEqual(result["atlas"]["size"], [14, 5])
        self.assertEqual(result["sequence"], [0, 1])
        self.assertEqual(result["webp"]["total_duration_ms"], 500)
        self.assertFalse(result["validation"]["visual_approval"])
        self.assertEqual(result["provenance"]["user_manifest"]["prompt"], "A quiet harbor")
        self.assertEqual([path.read_bytes() for path in paths], originals)
        for index, expected in enumerate([first, second]):
            with Image.open(self.output / "frames" / f"frame-{index:02d}.png") as saved:
                self.assertEqual(saved.size, expected.size)
                np.testing.assert_array_equal(np.asarray(saved), np.asarray(expected))
        self.assertTrue(result["transitions"][-1]["wrap"])
        self.assertGreater(result["transitions"][-1]["premultiplied_rgb_mae"], 0)
        metadata = json.loads((self.output / "animation.json").read_text(encoding="utf-8"))
        self.assertNotIn(".stage-", json.dumps(metadata))
        self.assertEqual(metadata["sources"][0]["file_sha256"], MODULE.digest(originals[0]))

    def test_alpha_and_hidden_rgb_survive_png_and_atlas_without_a_mask(self) -> None:
        pixels = np.zeros((5, 9, 4), dtype=np.uint8)
        pixels[:] = (60, 70, 80, 0)  # Hidden RGB must still survive PNG and atlas.
        pixels[1:4, 2:7] = (255, 0, 255, 128)
        pixels[2, 4] = (20, 100, 240, 255)
        altered = pixels.copy()
        altered[2, 4] = (40, 80, 200, 128)
        paths = [self.png(f"alpha-{index}.png", Image.fromarray(array))
                 for index, array in enumerate([pixels, altered])]
        result = MODULE.assemble(self.args("--input", *map(str, paths)))
        self.assertTrue(result["webp"]["alpha_and_visible_rgb_exact"])
        with Image.open(self.output / "atlas.png") as atlas:
            np.testing.assert_array_equal(np.asarray(atlas.crop((0, 0, 9, 5))), pixels)
            np.testing.assert_array_equal(np.asarray(atlas.crop((9, 0, 18, 5))), altered)
        with Image.open(self.output / "frames/frame-00.png") as frame:
            np.testing.assert_array_equal(np.asarray(frame), pixels)

    def test_sheet_grid_slices_rectangles_without_alignment(self) -> None:
        sheet = Image.new("RGBA", (10, 3), (20, 50, 100, 100))
        sheet.paste((200, 0, 180, 220), (5, 0, 10, 3))
        path = self.png("sheet.png", sheet)
        result = MODULE.assemble(self.args("--sheet", str(path), "--rows", "1", "--cols", "2"))
        self.assertEqual(result["frame_size"], [5, 3])
        self.assertEqual(result["frames"][1]["crop_box"], [5, 0, 10, 3])
        with Image.open(self.output / "frames/frame-01.png") as frame:
            np.testing.assert_array_equal(np.asarray(frame), np.asarray(sheet.crop((5, 0, 10, 3))))

    def test_explicit_crop_boxes_support_nonuniform_gaps(self) -> None:
        sheet = Image.new("RGB", (11, 7), (250, 2, 3))
        sheet.paste((20, 50, 80), (1, 1, 5, 4))
        sheet.paste((150, 200, 80), (7, 3, 11, 6))
        path = self.png("gaps.png", sheet)
        boxes = self.root / "boxes.json"
        boxes.write_text("[[1,1,5,4],[7,3,11,6]]", encoding="utf-8")
        result = MODULE.assemble(self.args("--sheet", str(path), "--crop-boxes", str(boxes)))
        self.assertEqual(result["frame_size"], [4, 3])
        self.assertEqual(result["frames"][1]["crop_box"], [7, 3, 11, 6])

    def test_nondivisible_sheet_is_rejected_without_publication(self) -> None:
        path = self.png("unequal.png", Image.new("RGB", (9, 5)))
        with self.assertRaisesRegex(ValueError, "not divisible"):
            MODULE.assemble(self.args("--sheet", str(path), "--rows", "2", "--cols", "2"))
        self.assert_clean_failure()

    def test_mismatched_input_sizes_are_rejected(self) -> None:
        first = self.png("first.png", Image.new("RGB", (7, 5)))
        second = self.png("second.png", Image.new("RGB", (8, 5)))
        with self.assertRaisesRegex(ValueError, "dimensions differ"):
            MODULE.assemble(self.args("--input", str(first), str(second)))
        self.assert_clean_failure()

    def test_crop_bounds_and_sizes_are_validated(self) -> None:
        path = self.png("sheet.png", Image.new("RGB", (9, 5)))
        for boxes, message in [
            ("[[0,0,10,5]]", "outside"),
            ("[[0,0,4,5],[4,0,9,5]]", "dimensions differ"),
            ("[[0,0,0,5]]", "empty"),
            ("[[false,0,4,5]]", "integers"),
        ]:
            with self.subTest(boxes=boxes), self.assertRaisesRegex(ValueError, message):
                MODULE.assemble(self.args("--sheet", str(path), "--crop-boxes", boxes))
            self.assert_clean_failure()

    def test_duplicate_sequence_retains_decoded_total_and_each_hold(self) -> None:
        first = self.png("first.png", Image.new("RGB", (7, 5), (255, 20, 90)))
        second = self.png("second.png", Image.new("RGB", (7, 5), (0, 180, 20)))
        result = MODULE.assemble(self.args("--input", str(first), str(second),
                                           "--sequence", "0,0,1,1,0", "--duration", "125"))
        self.assertEqual(result["sequence"], [0, 0, 1, 1, 0])
        self.assertEqual(result["total_duration_ms"], 625)
        self.assertEqual(result["webp"]["total_duration_ms"], 625)
        self.assertEqual(sum(frame["duration_ms"] for frame in result["webp"]["decoded_frames"]), 625)
        self.assertLess(result["webp"]["decoded_frame_count"], 5)
        self.assertTrue(result["webp"]["timeline_verified"])

    def test_all_identical_frames_keep_timed_animation_including_alpha(self) -> None:
        path = self.png("same.png", Image.new("RGBA", (7, 5), (50, 90, 170, 100)))
        result = MODULE.assemble(self.args("--input", str(path), "--sequence", "0,0,0", "--duration", "137"))
        self.assertTrue(result["webp"]["timed_static_container"])
        self.assertEqual(result["webp"]["decoded_frame_count"], 1)
        self.assertEqual(result["webp"]["decoded_frames"][0]["duration_ms"], 411)
        self.assertEqual(result["webp"]["loop"], 0)

    def test_single_frame_is_a_timed_webp(self) -> None:
        path = self.png("still.png", Image.new("RGB", (3, 5), (0, 80, 180)))
        result = MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(result["webp"]["total_duration_ms"], 250)
        self.assertEqual(result["transitions"][0]["changed_visible_pixels"], 0)

    def test_existing_output_is_never_changed_even_when_empty(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(list(self.output.iterdir()), [])
        marker = self.output / "keep.txt"
        marker.write_bytes(b"must remain")
        with self.assertRaises(FileExistsError):
            MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual(marker.read_bytes(), b"must remain")

    def test_encoder_failure_removes_stage_and_publishes_nothing(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        with mock.patch.object(MODULE, "encode_webp", side_effect=ValueError("injected decode failure")):
            with self.assertRaisesRegex(ValueError, "injected decode failure"):
                MODULE.assemble(self.args("--input", str(path)))
        self.assert_clean_failure()

    def test_competing_output_at_publication_is_preserved(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        publish = MODULE.forge_core.publish_directory_no_replace

        def competing_publish(stage: Path, final: Path) -> None:
            final.mkdir()
            (final / "someone-else.txt").write_bytes(b"keep")
            publish(stage, final)

        with mock.patch.object(MODULE.forge_core, "publish_directory_no_replace", side_effect=competing_publish):
            with self.assertRaises(FileExistsError):
                MODULE.assemble(self.args("--input", str(path)))
        self.assertEqual((self.output / "someone-else.txt").read_bytes(), b"keep")
        self.assertEqual(list(self.root.glob(".output.stage-*")), [])

    def test_duration_sequence_and_option_combinations_reject_without_output(self) -> None:
        path = self.png("first.png", Image.new("RGB", (7, 5)))
        for extra in [("--duration", "0"), ("--sequence", "-1"), ("--sequence", "0,2"),
                      ("--sequence", "0,"), ("--rows", "1", "--cols", "1")]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                MODULE.assemble(self.args("--input", str(path), *extra))
            self.assert_clean_failure()

    def test_static_regions_are_diagnostics_and_do_not_modify_frames(self) -> None:
        first = Image.new("RGB", (7, 5), (50, 80, 110))
        second = first.copy()
        second.putpixel((6, 4), (230, 10, 40))
        paths = [self.png("one.png", first), self.png("two.png", second)]
        result = MODULE.assemble(self.args("--input", *map(str, paths),
                                           "--static-regions", '{"wall":[0,0,3,3],"motion":[6,4,7,5]}'))
        self.assertEqual(result["static_regions"]["wall"]["against_reference"][1]["changed_visible_pixels"], 0)
        self.assertEqual(result["static_regions"]["motion"]["against_reference"][1]["changed_visible_pixels"], 1)
        self.assertFalse(result["static_regions"]["wall"]["registration_performed"])
        self.assertFalse(result["validation"]["visual_approval"])

    def test_decoder_validation_catches_wrong_timeline_and_pixels(self) -> None:
        frame = Image.new("RGB", (7, 5), (0, 80, 100))
        path = self.root / "single.webp"
        MODULE._timed_static_webp(path, frame, 250)
        with self.assertRaisesRegex(ValueError, "duration"):
            MODULE.validate_webp(path, [MODULE.pixel_array(frame)], [0, 0], 250)
        other = Image.new("RGB", (7, 5), (1, 80, 100))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            MODULE.validate_webp(path, [MODULE.pixel_array(other)], [0], 250)


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"

    def png(self, name: str, image: Image.Image, **options: object) -> Path:
        path = self.root / name
        image.save(path, **options)
        return path

    def assemble(self, *arguments: object) -> dict:
        args = MODULE.build_parser().parse_args([*map(str, arguments), "--output-dir", str(self.output)])
        return MODULE.assemble(args)

    def assert_clean_failure(self) -> None:
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".output.stage-*")), [])

    def read_frame(self, index: int) -> np.ndarray:
        with Image.open(self.output / "frames" / f"frame-{index:02d}.png") as frame:
            return np.asarray(frame.convert("RGBA"))


class LosslessInputTests(_Workspace):
    """S23: palette, low-bit, grey and 16-bit PNGs are expanded without loss instead of refused."""

    def test_indexed_png_accepted(self) -> None:
        palette = Image.new("P", (16, 12))
        palette.putpalette([0, 0, 0, 230, 40, 40, 30, 200, 60, 30, 30, 220] + [0] * 756)
        indices = np.zeros((12, 16), np.uint8)
        indices[2:10, 3:12] = 1
        indices[4:6, 5:8] = 2
        indices[8, 4] = 3
        palette.putdata(indices.ravel().tolist())
        path = self.png("indexed.png", palette, transparency=0, bits=2)
        self.assertEqual(path.read_bytes()[24], 2)  # a 2-bit palette PNG, as Pillow writes small palettes
        result = self.assemble("--input", path, path, "--sequence", "0,1")
        source = result["sources"][0]
        self.assertEqual((source["source_mode"], source["bit_depth"], source["mode"]), ("P", 2, "RGBA"))
        self.assertIn("tRNS", source["conversion"])
        lookup = np.array([[0, 0, 0, 0], [230, 40, 40, 255], [30, 200, 60, 255], [30, 30, 220, 255]], np.uint8)
        np.testing.assert_array_equal(self.read_frame(0), lookup[indices])
        assert_contract(self, json.loads((self.output / "animation.json").read_text(encoding="utf-8")),
                        "sprite", "full_frames_v2")

    @staticmethod
    def write_rgba16_png(path: Path, samples: np.ndarray) -> None:
        """A 16-bit RGBA PNG written by hand (Pillow cannot encode one)."""
        height, width = samples.shape[:2]
        scanlines = b"".join(b"\x00" + samples[row].astype(">u2").tobytes() for row in range(height))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        header = struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanlines))
                         + chunk(b"IEND", b""))

    def test_sixteen_bit_and_grey_inputs_expand_to_8_bit(self) -> None:
        wide = np.zeros((6, 5, 4), np.uint16)
        wide[1:5, 1:4] = (0xAB12, 0x3401, 0xFFFF, 0x8080)
        rgba16 = self.root / "rgba16.png"
        self.write_rgba16_png(rgba16, wide)
        grey16 = self.png("grey16.png", Image.fromarray(np.full((6, 5), 0x1234, np.uint16)))
        grey_alpha = Image.new("LA", (5, 6), (90, 0))
        grey_alpha.putpixel((2, 3), (200, 255))
        la = self.png("la.png", grey_alpha)
        result = self.assemble("--input", rgba16, grey16, la)
        sources = result["sources"]
        self.assertEqual([(source["bit_depth"], source["mode"]) for source in sources],
                         [(16, "RGBA"), (16, "RGBA"), (8, "RGBA")])
        self.assertIn(sources[1]["source_mode"], ("I;16", "I"))
        self.assertEqual(sources[2]["source_mode"], "LA")
        self.assertEqual(self.read_frame(0)[2, 2].tolist(), [0xAB, 0x34, 0xFF, 0x80])  # high bytes
        self.assertEqual(self.read_frame(0)[0, 0].tolist(), [0, 0, 0, 0])
        self.assertEqual(self.read_frame(1)[0, 0].tolist(), [0x12, 0x12, 0x12, 255])
        self.assertEqual(self.read_frame(2)[3, 2].tolist(), [200, 200, 200, 255])
        self.assertEqual(int(self.read_frame(2)[0, 0, 3]), 0)
        assert_contract(self, result, "sprite", "full_frames_v2")

    def test_rgb_stays_rgb_and_animated_png_is_refused(self) -> None:
        still = self.png("still.png", Image.new("RGB", (4, 3), (10, 20, 30)))
        result = self.assemble("--input", still)
        self.assertEqual(result["frames"][0]["mode"], "RGB")
        animated = self.root / "animated.png"
        Image.new("RGB", (4, 3), 1).save(animated, save_all=True, append_images=[Image.new("RGB", (4, 3), 2)])
        self.output = self.root / "animated-out"
        with self.assertRaisesRegex(ValueError, "animated"):
            self.assemble("--input", animated)
        self.assert_clean_failure()
        self.output = self.root / "jpeg-out"
        jpeg = self.root / "frame.jpg"
        Image.new("RGB", (4, 3)).save(jpeg)
        with self.assertRaisesRegex(ValueError, "must be a PNG"):
            self.assemble("--input", jpeg)


class ChromaAndCropBoxTests(_Workspace):
    """DOC-03 (optional keying through forge_matte.key_still) and DOC-18 (one crop-box schema)."""

    def test_chroma_crop_boxes_keyed(self) -> None:
        sheet, subjects = make_magenta_sheet(2, 2, 40, return_boxes=True)
        path = self.png("sheet.png", sheet)
        items = {"schema": "forge-crop-boxes/v1",
                 "items": [{"id": name, "box": box} for name, box in
                           zip(("idle", "step", "jump", "land"), ([0, 0, 40, 40], [40, 0, 80, 40], [0, 40, 40, 80],
                                                                   [40, 40, 80, 80]))]}
        assert_contract(self, items, "common", "cropBoxes")
        result = self.assemble("--sheet", path, "--crop-boxes", json.dumps(items), "--key", "chroma")
        self.assertEqual([frame["crop_id"] for frame in result["frames"]], ["idle", "step", "jump", "land"])
        self.assertEqual(result["slicing"]["crop_box_format"], "items")
        self.assertTrue(result["processing"]["chroma_keyed"])
        key = result["sources"][0]["key"]
        self.assertEqual(key["qa"]["opaque_key_px"], 0)
        self.assertEqual(key["key_rgb"], [255, 0, 255])
        for index, (x0, y0, x1, y1) in enumerate(subjects):
            frame = self.read_frame(index)
            cell_x, cell_y = (index % 2) * 40, (index // 2) * 40
            self.assertEqual(frame[0, 0].tolist(), [0, 0, 0, 0])
            self.assertTrue((frame[y0 - cell_y + 2:y1 - cell_y - 2, x0 - cell_x + 2:x1 - cell_x - 2, 3] == 255).all())
            magenta = (frame[..., 3] > 0) & (frame[..., 0] > 200) & (frame[..., 1] < 60) & (frame[..., 2] > 200)
            self.assertFalse(magenta.any())
        checks = {check["id"]: check for check in result["qa"]["checks"]}
        self.assertEqual((checks["chroma_key_residue"]["status"], checks["chroma_key_residue"]["value"]), ("pass", 0))
        self.assertEqual(checks["cross_cell_spill"]["status"], "pass")
        assert_contract(self, result, "sprite", "full_frames_v2")

    def test_green_key_pixel_art_needs_a_soft_or_dominance_keyer(self) -> None:
        path = self.png("green.png", make_magenta_sheet(1, 2, 32, key=(0, 255, 0)))
        with self.assertRaisesRegex(ValueError, "pass --key-quality dominance or soft"):
            self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--key", "chroma", "--key-color", "green",
                          "--pixel-art")
        self.assert_clean_failure()
        result = self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--key", "chroma", "--key-color", "green",
                               "--pixel-art", "--key-quality", "dominance")
        self.assertEqual(result["sources"][0]["key"]["quality"], "dominance")
        self.assertEqual(int(self.read_frame(0)[0, 0, 3]), 0)

    def test_chroma_key_without_a_backdrop_warns(self) -> None:
        path = self.png("scene.png", Image.new("RGB", (32, 16), (90, 120, 60)))
        result = self.assemble("--input", path, "--key", "chroma")
        self.assertFalse(result["sources"][0]["key"]["key_estimate"]["valid"])
        checks = {check["id"]: check for check in result["qa"]["checks"]}
        self.assertEqual((checks["chroma_key_backdrop"]["status"], checks["chroma_key_backdrop"]["value"]), ("warn", 1))
        self.assertEqual(int(self.read_frame(0)[..., 3].min()), 255)  # nothing near the key: nothing keyed
        self.assertEqual(result["qa"]["status"], "warn")

    def test_documented_crop_box_example_is_valid(self) -> None:
        text = (SKILLS_DIR / "generate2dsprite" / "references" / "frames-and-clips.md").read_text(encoding="utf-8")
        blocks = [block.split("```", 1)[0] for block in text.split("```json\n")[1:]]
        example = json.loads(next(block for block in blocks if '"items"' in block))
        assert_contract(self, example, "common", "cropBoxes")
        boxes, form = MODULE.parse_crop_boxes(example, (768, 512))
        self.assertEqual((form, [item_id for item_id, _ in boxes]), ("items", ["walk-0", "walk-1"]))

    def test_legacy_crop_box_aliases_give_the_same_frames(self) -> None:
        sheet = self.png("sheet.png", make_magenta_sheet(1, 2, 32))
        boxes = [[0, 0, 32, 32], [32, 0, 64, 32]]
        forms = {"legacy-list": boxes,
                 "legacy-props": {"props": [{"label": "a", "source_box": boxes[0]}, {"label": "b", "source_box": boxes[1]}]},
                 "items": {"items": [{"id": "a", "box": boxes[0]}, {"box": boxes[1]}]}}
        hashes = {}
        for form, value in forms.items():
            with self.subTest(form=form):
                self.output = self.root / form
                result = self.assemble("--sheet", sheet, "--crop-boxes", json.dumps(value))
                self.assertEqual(result["slicing"]["crop_box_format"], form)
                hashes[form] = [frame["rgba_pixel_sha256"] for frame in result["frames"]]
        self.assertEqual(len({tuple(value) for value in hashes.values()}), 1)
        self.output = self.root / "duplicate-ids"
        with self.assertRaisesRegex(ValueError, "unique"):
            self.assemble("--sheet", sheet, "--crop-boxes", json.dumps({"items": [{"id": "a", "box": boxes[0]},
                                                                                 {"id": "a", "box": boxes[1]}]}))
        with self.assertRaisesRegex(ValueError, "crop-boxes must be"):
            self.assemble("--sheet", sheet, "--crop-boxes", json.dumps({"boxes": boxes}))
        self.assert_clean_failure()


class SpillAndOwnershipTests(_Workspace):
    """Report v2 P1-3: a tail crossing its cell is caught before slicing; ownership keeps it whole."""

    def test_cross_cell_tail_rejected_and_ownership_slices(self) -> None:
        sheet, subjects = make_magenta_sheet(1, 3, 48, spill_px=6, return_boxes=True)
        path = self.png("spill.png", sheet)
        with self.assertRaisesRegex(ValueError, r"Cross-cell spill.*owned by r0c0.*overhang 6 px.*--slice ownership"):
            self.assemble("--sheet", path, "--rows", 1, "--cols", 3, "--key", "chroma")
        self.assert_clean_failure()
        result = self.assemble("--sheet", path, "--rows", 1, "--cols", 3, "--key", "chroma", "--slice", "ownership")
        spill = result["spill_check"]
        self.assertEqual(spill["status"], "resolved")
        self.assertEqual([(item["owner_id"], item["overhang_px"]) for item in spill["crossing"]], [("r0c0", 6)])
        self.assertEqual(result["slicing"]["padding"], [0, 0, 6, 0])
        self.assertEqual(result["frame_size"], [54, 48])
        self.assertEqual([frame["sheet_origin"] for frame in result["frames"]], [[0, 0], [48, 0], [96, 0]])
        tail = self.read_frame(0)
        x0, y0, x1, y1 = subjects[0]
        self.assertEqual(x1, 54)
        self.assertTrue((tail[y0 + 1:y1 - 1, 48:53, 3] == 255).all(), "the tail beyond the cell line is kept")
        neighbour = self.read_frame(1)
        self.assertEqual(int(neighbour[:, :8, 3].sum()), 0, "the neighbour no longer holds the tail")
        with Image.open(path) as original:
            options = argparse.Namespace(key_quality="auto", key_color="auto", pixel_art=False)
            keyed = np.asarray(MODULE.key_image(original, options)[0])
        np.testing.assert_array_equal(tail, keyed[:, :54])  # cell 0 plus its tail, pixels unchanged
        over = int((keyed[:, 48:54, 3] > 0).sum())
        self.assertGreaterEqual(over, 6 * (y1 - y0))
        self.assertEqual(result["frames"][0]["pixels_from_outside_cell"], over)
        assert_contract(self, result, "sprite", "full_frames_v2")

    def test_allow_spill_cuts_with_a_warning_and_strict_refuses(self) -> None:
        path = self.png("spill.png", make_magenta_sheet(1, 2, 40, spill_px=4))
        result = self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--key", "chroma", "--allow-spill")
        self.assertEqual(result["spill_check"]["status"], "allowed")
        self.assertEqual(result["qa"]["status"], "warn")
        self.output = self.root / "strict"
        with self.assertRaisesRegex(ValueError, "--strict"):
            self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--key", "chroma", "--allow-spill", "--strict")
        self.assert_clean_failure()

    def test_opaque_sheet_skips_the_spill_check(self) -> None:
        path = self.png("scene.png", make_magenta_sheet(1, 2, 32))
        result = self.assemble("--sheet", path, "--rows", 1, "--cols", 2)
        self.assertEqual(result["spill_check"]["status"], "skipped")
        self.assertEqual(result["processing"]["chroma_keyed"], False)
        self.output = self.root / "own"
        with self.assertRaisesRegex(ValueError, "transparent background"):
            self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--slice", "ownership")
        self.assert_clean_failure()

    def test_ownership_handles_rounded_cells_of_a_nondivisible_sheet(self) -> None:
        sheet = np.zeros((10, 21, 4), np.uint8)
        sheet[2:8, 1:6] = (200, 30, 30, 255)
        sheet[2:8, 8:13] = (30, 200, 30, 255)
        sheet[2:8, 15:20] = (30, 30, 200, 255)
        path = self.png("odd.png", Image.fromarray(sheet))
        result = self.assemble("--sheet", path, "--rows", 1, "--cols", 2, "--slice", "ownership")
        self.assertEqual(result["slicing"]["rounding"], "half-up (forge_core.rounded_grid_boxes)")
        self.assertEqual([frame["crop_box"] for frame in result["frames"]], [[0, 0, 11, 10], [11, 0, 21, 10]])
        self.assertEqual(result["frame_size"], [13, 10])  # the middle block belongs to cell 0: 2 px past x = 11
        np.testing.assert_array_equal(self.read_frame(0)[2:8, 8:13], sheet[2:8, 8:13])

    def test_fox_sheet_spill_reported_and_tail_kept_whole(self) -> None:
        """Real fixture (PROVENANCE.json): frame 3's tail crosses x = 1152 (report v2 P1-3)."""
        fox = real_fixture("raw-fox-run-v1.png")
        with self.assertRaisesRegex(ValueError, r"52263 px.*owned by r0c3, with 77 px outside it \(overhang 7 px\)"):
            self.assemble("--sheet", fox, "--rows", 2, "--cols", 4)
        self.assert_clean_failure()
        with Image.open(fox) as image:
            rgba = np.asarray(image.convert("RGBA"))
        boxes = [(f"c{index}", box) for index, box in enumerate(MODULE.forge_core.rounded_grid_boxes(1536, 1024, 2, 4))]
        report = MODULE.spill_report(rgba[..., 3], boxes)
        self.assertEqual([(item["owner_id"], item["pixels_over"]) for item in report["crossing"]], [("c3", 77)])
        frames, slicing, records = MODULE.ownership_slice(rgba, boxes)
        pad_left, pad_top = slicing["padding"][:2]
        self.assertEqual(records[3]["sheet_origin"], [1152 - pad_left, -pad_top])
        labels, _ = MODULE.forge_core.label_components(rgba[..., 3] > 16, 8)
        ys, xs = np.nonzero(labels == report["crossing"][0]["label"])
        np.testing.assert_array_equal(frames[3][ys + pad_top, xs - 1152 + pad_left], rgba[ys, xs])  # whole tail
        crossing = xs < 1152
        self.assertEqual(int(crossing.sum()), 77)
        self.assertFalse(frames[2][ys[crossing] + pad_top, xs[crossing] - 768 + pad_left, 3].any())


class LoopToolTests(_Workspace):
    """Loop and landmark QC (asf-improved) and the Dusk forward-overlap crossfade (build-scenes.py L56)."""

    @staticmethod
    def drifting_loop(count: int = 16) -> list[Image.Image]:
        """Smooth ambient motion that does not return to its start: small steps, a pop at the wrap."""
        xs = np.arange(64)[None, :]
        frames = []
        for index in range(count):
            value = 128 + 60 * np.sin(2 * np.pi * (xs / 32 + index / 40)) + 2 * index
            row = np.clip(np.floor(value + 0.5), 0, 255).astype(np.uint8)
            frames.append(Image.fromarray(np.repeat(np.repeat(row[..., None], 3, axis=2), 24, axis=0)))
        return frames

    def test_static_region_shift_detected(self) -> None:
        texture = np.random.default_rng(5).integers(0, 256, (60, 80, 3), dtype=np.uint8)
        moved = np.roll(texture, (-1, 2), axis=(0, 1))  # the wall's content moved 2 px right and 1 px up
        paths = [self.png("a.png", Image.fromarray(texture)), self.png("b.png", Image.fromarray(moved)),
                 self.png("c.png", Image.fromarray(texture))]
        result = self.assemble("--input", *paths, "--static-regions", '{"wall": [20, 15, 50, 40]}',
                               "--static-region-search", 4)
        wall = result["static_regions"]["wall"]
        drift = [row["drift"] for row in wall["against_reference"]]
        self.assertEqual([item["shift"] for item in drift], [[0, 0], [2, -1], [0, 0]])
        self.assertEqual(drift[1]["ncc"], 1.0)
        self.assertLess(drift[1]["ncc_at_zero"], 0.5)
        self.assertEqual((wall["max_drift_px"], wall["drifting_frames"]), (2, [1]))
        self.assertEqual(result["qa"]["checks"][-1]["id"], "static_region_drift")
        self.assertEqual(result["qa"]["checks"][-1]["status"], "warn")
        self.assertFalse(wall["registration_performed"])

    def test_flat_static_region_reports_no_texture(self) -> None:
        flat = self.png("flat.png", Image.new("RGB", (20, 20), (90, 90, 90)))
        result = self.assemble("--input", flat, flat, "--static-regions", '{"sky": [2, 2, 12, 12]}',
                               "--static-region-search", 3)
        self.assertIn("flat region", result["static_regions"]["sky"]["against_reference"][1]["drift"]["reason"])
        self.output = self.root / "no-regions"
        with self.assertRaisesRegex(ValueError, "needs --static-regions"):
            self.assemble("--input", flat, "--static-region-search", 3)

    def test_loop_overlap_reduces_seam_ratio_below_1(self) -> None:
        paths = [self.png(f"water-{index}.png", frame) for index, frame in enumerate(self.drifting_loop())]
        plain = self.assemble("--input", *paths)
        self.assertGreater(plain["loop_seam"]["wrap_ratio"], 3.0, "the raw loop pops at the wrap")
        self.output = self.root / "overlap"
        result = self.assemble("--input", *paths, "--loop-overlap", 4, "--ambient")
        overlap = result["loop_overlap"]
        self.assertAlmostEqual(overlap["seam_before"]["wrap_ratio"], plain["loop_seam"]["wrap_ratio"])
        self.assertLess(overlap["seam_after"]["wrap_ratio"], 1.0)
        self.assertEqual(result["loop_seam"], overlap["seam_after"])
        self.assertEqual(result["sequence"], [4, 5, 6, 7, 8, 9, 10, 11, 16, 17, 18, 3])
        self.assertEqual(result["total_duration_ms"], 12 * 250)
        derived = [frame["derived"] for frame in result["frames"][16:]]
        self.assertEqual([item["blend_of"] for item in derived], [[12, 0], [13, 1], [14, 2]])
        self.assertEqual([item["weight"] for item in derived], [0.15625, 0.5, 0.84375])  # smoothstep(j/4)
        frames = [np.asarray(frame.convert("RGBA")) for frame in self.drifting_loop()]
        np.testing.assert_array_equal(self.read_frame(17)[..., :3],
                                      MODULE.blend_rgba(frames[13], frames[1], 0.5)[..., :3])
        self.assertTrue(result["processing"]["blended"])
        assert_contract(self, result, "sprite", "full_frames_v2")

    def test_loop_overlap_is_for_ambient_loops_only(self) -> None:
        paths = [self.png(f"f{index}.png", frame) for index, frame in enumerate(self.drifting_loop(6))]
        with self.assertRaisesRegex(ValueError, "ambient loops only.*Never crossfade characters"):
            self.assemble("--input", *paths, "--loop-overlap", 2)
        with self.assertRaisesRegex(ValueError, "at least 5 played positions"):
            self.assemble("--input", *paths[:4], "--loop-overlap", 2, "--ambient")
        with self.assertRaisesRegex(ValueError, "at least 2 frames"):
            self.assemble("--input", *paths, "--loop-overlap", 1, "--ambient")
        self.assert_clean_failure()

    def test_wrap_ratio_flags_a_duplicated_end_frame(self) -> None:
        paths = [self.png(f"f{index}.png", frame) for index, frame in enumerate(self.drifting_loop(4))]
        result = self.assemble("--input", *paths, "--sequence", "0,1,2,3,0")
        self.assertEqual(result["loop_seam"]["seam"], 0.0)
        self.assertEqual(result["loop_seam"]["wrap_ratio"], 0.0)
        seam_check = next(check for check in result["qa"]["checks"] if check["id"] == "loop_seam")
        self.assertEqual(seam_check["status"], "needs-visual-review")


class WebpEncoderTests(_Workspace):
    """libwebp 1.6's animation encoder drops the VP8X alpha flag when it crops a fully opaque first
    frame out of a canvas whose hidden RGB is black; cfed170 then refused these valid frames."""

    def opaque_block_frames(self) -> list[Path]:
        paths = []
        for index in range(2):
            pixels = np.zeros((24, 20, 4), np.uint8)
            pixels[4:24, 4:10] = (30, 160, 90, 255)
            pixels[8, 4 + index] = (250, 250, 250, 255)
            paths.append(self.png(f"block-{index}.png", Image.fromarray(pixels)))
        return paths

    def test_opaque_block_frames_keep_alpha_in_the_webp(self) -> None:
        result = self.assemble("--input", *self.opaque_block_frames())
        self.assertIsInstance(result["webp"]["all_keyframes"], bool)
        with Image.open(self.output / "animation.webp") as decoded:
            self.assertEqual(decoded.n_frames, 2)
            for index in range(2):
                decoded.seek(index)
                np.testing.assert_array_equal(np.asarray(decoded.convert("RGBA"))[..., 3], self.read_frame(index)[..., 3])

    def test_failed_verification_is_reencoded_with_all_keyframes(self) -> None:
        real = MODULE.save_animation
        attempts = []

        def defective_first_attempt(path, images, duration, loop, *, all_keyframes=False):
            attempts.append(all_keyframes)
            if not all_keyframes:  # what the defect decodes to: the canvas painted opaque
                images = [image.convert("RGB") for image in images]
            real(path, images, duration, loop, all_keyframes=all_keyframes)

        with mock.patch.object(MODULE, "save_animation", side_effect=defective_first_attempt):
            result = self.assemble("--input", *self.opaque_block_frames())
        self.assertEqual(attempts, [False, True])
        self.assertTrue(result["webp"]["all_keyframes"])
        self.output = self.root / "always-defective"
        with mock.patch.object(MODULE, "save_animation", side_effect=lambda path, images, *rest, **options: real(
                path, [image.convert("RGB") for image in images], *rest, **options)):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                self.assemble("--input", *self.opaque_block_frames())
        self.assert_clean_failure()


class CliTests(_Workspace):
    """Appendix D: cp1252 help, refusal of an existing output, nothing published on a QC failure."""

    def test_help_works_under_cp1252_and_cp950(self) -> None:
        assert_cli_help("generate2dsprite", "assemble_frames")

    def test_cli_refuses_existing_output(self) -> None:
        frame = self.png("f.png", Image.new("RGB", (4, 4)))
        self.output.mkdir()
        (self.output / "keep.txt").write_text("keep", encoding="utf-8")
        result = run_cli([SCRIPT, "--input", frame, "--output-dir", self.output], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("error: Refusing existing output"), result.stderr)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["keep.txt"])

    def test_cli_spill_failure_publishes_nothing(self) -> None:
        sheet = self.png("spill.png", make_magenta_sheet(1, 2, 40, spill_px=4))
        result = run_cli([SCRIPT, "--sheet", sheet, "--rows", "1", "--cols", "2", "--key", "chroma",
                          "--output-dir", self.output], "cp1252")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: Cross-cell spill", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_clean_failure()

    def test_cli_success_prints_one_json_line_and_usage_errors_exit_1(self) -> None:
        frame = self.png("f.png", Image.new("RGB", (4, 4), (10, 20, 30)))
        result = run_cli([SCRIPT, "--input", frame, frame, "--output-dir", self.output], "cp1252")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        summary = json.loads(lines[0])
        self.assertEqual(Path(summary["output"]), self.output.resolve())
        self.assertTrue(Path(summary["metadata"]).is_file())
        self.assertEqual((summary["frames"], summary["sequence"]), (2, 2))
        usage = run_cli([SCRIPT, "--input", frame, "--output-dir", self.root / "x", "--slice", "wedge"], "cp1252")
        self.assertEqual(usage.returncode, 1)
        self.assertIn("error:", usage.stderr)


if __name__ == "__main__":
    unittest.main()
