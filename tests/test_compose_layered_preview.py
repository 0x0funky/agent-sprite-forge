"""Behavioral regression tests for map composition (compose_layered_preview)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from forge_testutils import (assert_cli_help, assert_valid_contract, contract_errors, load_script, run_cli,
                             script_path)


COMPOSE = load_script("generate2dmap", "compose_layered_preview")
SCRIPT = script_path("generate2dmap", "compose_layered_preview")
SKILL = "generate2dmap"

# handoff/B12-map-compose-parallax.md section 5 is integrated into shared/schemas/map.schema.json; the tests
# validate against the vendored generate2dmap copy.
def requested_contract_errors(document, name="compose_report_v2"):
    """Errors against the vendored generate2dmap schemas, which hold this module's section 5 requests."""
    return contract_errors(document, "map", name, skill=SKILL)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compose(*arguments, cwd=None):
    return run_cli([SCRIPT, *(str(argument) for argument in arguments)], cwd=cwd)


def run_main(argv):
    """main() in-process with its console output swallowed; returns the exit status."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return COMPOSE.main([str(argument) for argument in argv])


def rgba(path):
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA")).copy()


def padded_tree():
    """A 40x60 prop whose art (rows 4-51) ends 8 px above the canvas bottom, like an extracted padded prop."""
    tree = Image.new("RGBA", (40, 60))
    tree.paste((0, 160, 0, 255), (10, 4, 30, 52))
    return tree


def write_pack(root, items, folder="pack", schema=True):
    """A prop-pack.json beside <folder>/<label>/prop.png, as extract_prop_pack writes it (v2, or v1 without anchors)."""
    accepted = []
    for label, (image, anchor, extra) in items.items():
        (root / folder / label).mkdir(parents=True, exist_ok=True)
        image.save(root / folder / label / "prop.png")
        width, height = image.size
        item = {"label": label, "display_name": label, "image": f"{label}/prop.png",
                "sha256": sha256(root / folder / label / "prop.png"), "source_rect": [0, 0, width, height],
                "cell_box": [0, 0, width, height], "padding": [0, 0, 0, 0], "anchor_px": list(anchor),
                "dropped_components": 0, "dropped_area": 0, "status": "accepted", **extra}
        if not schema:
            item = {"label": label, "image": f"{label}/prop.png", "status": "accepted"}
        accepted.append(item)
    manifest = {"accepted": accepted, "rejected": []}
    if schema:
        manifest = {"schema": "generate2dmap.prop_pack.v2", **manifest}
    (root / folder / "prop-pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root / folder / "prop-pack.json", manifest


class CompositionTests(unittest.TestCase):
    def test_collects_all_layers_including_actor(self):
        entries = COMPOSE.load_props({"props": [{"id": "tree"}], "objects": [{"id": "chest"}],
                                     "actors": [{"id": "hero"}], "foreground": [{"id": "roof"}]})
        self.assertEqual([item["id"] for item in entries], ["tree", "chest", "hero", "roof"])
        self.assertEqual(entries[-1]["layer"], "foreground")

    def test_source_anchor_scales_with_art(self):
        self.assertEqual(COMPOSE.placement_xy({"x": 80, "y": 100, "anchorPx": [10, 30]}, 40, 80, (20, 40)), (60, 40))

    def test_invalid_anchor_fails(self):
        for prop in ({"anchor": "misspelled"}, {"anchorPx": [2, 50]}, {"anchorPx": [float("nan"), 0]}):
            with self.subTest(prop=prop), self.assertRaises(ValueError):
                COMPOSE.placement_xy(prop, 20, 40)

    def test_nearest_preserves_alpha_and_clipping_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Image.new("RGBA", (2, 2), (255, 0, 255, 128))
            source.putpixel((1, 1), (0, 0, 0, 0))
            source.save(root / "prop.png")
            canvas = Image.new("RGBA", (10, 10))
            report = COMPOSE.paste_prop(canvas, {"image": "prop.png", "w": 4, "h": 4, "x": -1, "y": 0, "anchor": "top-left"}, [root], "nearest")
            self.assertEqual(canvas.getpixel((0, 0)), (255, 0, 255, 128))
            self.assertTrue(report["clipped"])

    def test_rejects_invalid_opacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (2, 2)).save(root / "p.png")
            with self.assertRaises(ValueError):
                COMPOSE.paste_prop(Image.new("RGBA", (4, 4)), {"image": "p.png", "opacity": float("nan")}, [root])

    def test_palette_png_props_load_as_rgba(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            palette = Image.new("P", (2, 1))
            palette.putpalette([0, 0, 0, 200, 30, 40])
            palette.putdata([0, 1])
            palette.save(root / "p.png", transparency=0)
            canvas = Image.new("RGBA", (2, 1), (9, 9, 9, 255))
            COMPOSE.paste_prop(canvas, {"image": "p.png", "anchor": "top-left"}, [root])
            self.assertEqual([canvas.getpixel((0, 0)), canvas.getpixel((1, 0))], [(9, 9, 9, 255), (200, 30, 40, 255)])


class ForkLayeredPreviewTests(unittest.TestCase):
    """The improved fork's five compose tests (B12-T1), bodies unchanged except module loading. The last
    one encodes the cfed170 raw-y sortY, so it now runs under the legacy switch and also checks the default."""

    def test_all_render_arrays_reach_output_and_foreground_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGBA', (8, 8), 'black').save(root / 'base.png')
            for name, color in [('tree', 'green'), ('actor', 'red'), ('roof', 'blue')]:
                Image.new('RGBA', (2, 2), color).save(root / f'{name}.png')
            data = {'props': [{'image': 'tree.png', 'x': 2, 'y': 2}],
                    'actors': [{'image': 'actor.png', 'x': 2, 'y': 2}],
                    'objects': [{'image': 'actor.png', 'x': 5, 'y': 5}],
                    'foreground': [{'image': 'roof.png', 'x': 2, 'y': 2, 'sortY': -20}]}
            (root / 'scene.json').write_text(json.dumps(data))
            run = run_cli([SCRIPT, '--base', str(root / 'base.png'),
                           '--placements', str(root / 'scene.json'), '--output', str(root / 'out.png'),
                           '--report', str(root / 'report.json')])
            self.assertEqual(run.returncode, 0, run.stderr)
            with Image.open(root / 'out.png') as image:
                self.assertEqual(image.getpixel((1, 1)), (0, 0, 255, 255))
                self.assertEqual(image.getpixel((4, 4)), (255, 0, 0, 255))
            self.assertEqual(len(json.loads((root / 'report.json').read_text())['pasted']), 4)

    def test_native_alpha_is_composited_once_and_nearest_preserves_palette(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = Image.new('RGBA', (2, 1))
            source.putdata([(200, 100, 50, 128), (0, 0, 255, 255)])
            source.save(root / 'prop.png')
            canvas = Image.new('RGBA', (4, 2), (0, 0, 0, 0))
            COMPOSE.paste_prop(canvas, {'image': 'prop.png', 'anchor': 'top-left', 'w': 4, 'h': 2},
                               [root], 'nearest')
            colors = lambda image: {image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}
            self.assertEqual(colors(canvas), colors(source))

    def test_source_contact_anchor_survives_scaling(self):
        self.assertEqual(COMPOSE.placement_xy({'x': 100, 'y': 80, 'anchorPx': [25, 40]},
                                              100, 100, (50, 50)), (50, 0))
        with self.assertRaises(ValueError):
            COMPOSE.placement_xy({'anchorPx': [51, 40]}, 100, 100, (50, 50))
        with self.assertRaises(ValueError):
            COMPOSE.placement_xy({'anchor': 'typo'}, 10, 10)

    def test_invalid_array_does_not_silently_drop_objects(self):
        with self.assertRaises(ValueError):
            COMPOSE.load_props({'props': [], 'foreground': {}})

    def test_report_sort_baseline_matches_sorting_when_y_is_omitted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Image.new('RGBA', (10, 10), 'red').save(root / 'prop.png')
            prop = {'image': 'prop.png', 'anchor': 'center'}
            report = COMPOSE.paste_prop(Image.new('RGBA', (20, 20)), prop, [root], sort='raw-y')
            self.assertEqual(report['sortY'], COMPOSE.effective_sort_y(prop))
            self.assertEqual(report['sortY'], 0)
            # Default (B12-T2): the ground line of a centre-anchored 10 px prop at y=0 is y + h/2.
            report = COMPOSE.paste_prop(Image.new('RGBA', (20, 20)), prop, [root])
            self.assertEqual(report['sortY'], COMPOSE.effective_sort_y(prop, 5.0))
            self.assertEqual((report['sortY'], report['sort_source']), (5, 'ground-line'))


class GroundLineSortTests(unittest.TestCase):
    """B12-T2: ground-line sort with sortY reported (MAP-05), ties by (sortY, x, id), golden draw order."""

    def repro_01_scene(self, root):
        Image.new("RGBA", (200, 200), (128, 128, 128, 255)).save(root / "base.png")
        Image.new("RGBA", (40, 120), (0, 160, 0, 255)).save(root / "tree.png")
        Image.new("RGBA", (60, 20), (200, 0, 0, 255)).save(root / "bush.png")
        for anchor, tree_y, bush_y in (("center", 100, 140), ("top-left", 40, 130)):
            tree_x, bush_x = (100, 100) if anchor == "center" else (80, 70)
            placements = {"props": [{"id": "tree", "image": "tree.png", "x": tree_x, "y": tree_y, "anchor": anchor},
                                    {"id": "bush", "image": "bush.png", "x": bush_x, "y": bush_y, "anchor": anchor}]}
            (root / f"placements-{anchor}.json").write_text(json.dumps(placements), encoding="utf-8")

    def test_repro_01_ground_line_draws_the_lower_base_in_front(self):
        """repro_01 (MAP-05): the tree's base (y=160) is below the bush's (y=150), so the tree is in front."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repro_01_scene(root)
            for anchor in ("center", "top-left"):
                with self.subTest(anchor=anchor):
                    run = compose("--base", root / "base.png", "--placements", root / f"placements-{anchor}.json",
                                  "--output", root / f"preview-{anchor}.png",
                                  "--report", root / f"report-{anchor}.json")
                    self.assertEqual(run.returncode, 0, run.stderr)
                    report = json.loads((root / f"report-{anchor}.json").read_text(encoding="utf-8"))
                    order = [(item["id"], item["top"] + item["h"], item["sortY"], item["sort_source"])
                             for item in report["pasted"]]
                    self.assertEqual(order, [("bush", 150, 150, "ground-line"), ("tree", 160, 160, "ground-line")])
                    self.assertEqual(tuple(rgba(root / f"preview-{anchor}.png")[140, 100]), (0, 160, 0, 255))

    def test_repro_01_legacy_raw_y_keeps_the_cfed170_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.repro_01_scene(root)
            run = compose("--base", root / "base.png", "--placements", root / "placements-center.json",
                          "--output", root / "legacy.png", "--report", root / "legacy.json", "--sort", "raw-y")
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((root / "legacy.json").read_text(encoding="utf-8"))
            self.assertEqual([(item["id"], item["sortY"], item["sort_source"]) for item in report["pasted"]],
                             [("tree", 100, "raw-y"), ("bush", 140, "raw-y")])
            self.assertEqual(tuple(rgba(root / "legacy.png")[140, 100]), (200, 0, 0, 255))

    def golden_scene(self, root):
        Image.new("RGBA", (100, 100), (90, 90, 90, 255)).save(root / "base.png")
        for name, size in (("block", (10, 10)), ("tall", (10, 20))):
            Image.new("RGBA", size, (200, 200, 200, 255)).save(root / f"{name}.png")
        data = {
            "props": [
                {"id": "rock", "image": "block.png", "x": 50, "y": 40, "anchor": "center"},
                {"id": "tree", "image": "tall.png", "x": 30, "y": 50, "anchorPx": [5, 20]},
                {"id": "bush-b", "image": "block.png", "x": 20, "y": 60},
                {"id": "bush-a", "image": "block.png", "x": 20, "y": 60},
                {"id": "bush-c", "image": "block.png", "x": 10, "y": 60},
                {"id": "shadow", "image": "block.png", "x": 50, "y": 99, "layer": "background"},
            ],
            "objects": [{"id": "chest", "image": "block.png", "x": 60, "y": 30, "anchor": "top-left"},
                        {"id": "sign", "image": "block.png", "x": 5, "y": 5, "sortY": 100}],
            "actors": [{"id": "hero", "image": "tall.png", "x": 40, "y": 55, "anchorPx": [5, 20]}],
            "foreground": [{"id": "roof", "image": "block.png", "x": 50, "y": 90, "sortY": -20},
                           {"id": "canopy", "image": "block.png", "x": 50, "y": 10}],
        }
        (root / "scene.json").write_text(json.dumps(data), encoding="utf-8")

    def test_golden_draw_order(self):
        """Bands first (background, world, foreground), then (sortY, x, id) inside each band."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.golden_scene(root)
            golden = {
                "ground-line": ["shadow", "chest", "rock", "tree", "hero", "bush-c", "bush-a", "bush-b", "sign",
                                "roof", "canopy"],
                "raw-y": ["shadow", "chest", "rock", "tree", "hero", "bush-b", "bush-a", "bush-c", "sign",
                          "roof", "canopy"],
            }
            for mode, expected in golden.items():
                with self.subTest(sort=mode):
                    run = compose("--base", root / "base.png", "--placements", root / "scene.json", "--output",
                                  root / f"{mode}.png", "--report", root / f"{mode}.json", "--sort", mode)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    report = json.loads((root / f"{mode}.json").read_text(encoding="utf-8"))
                    self.assertEqual([item["id"] for item in report["pasted"]], expected)
                    self.assertEqual([item["draw_index"] for item in report["pasted"]], list(range(len(expected))))
                    self.assertEqual([entry["band"] for entry in report["compositing_order"]][:4],
                                     ["base", "background", "world", "foreground"])
            report = json.loads((root / "ground-line.json").read_text(encoding="utf-8"))
            by_id = {item["id"]: item for item in report["pasted"]}
            self.assertEqual((by_id["chest"]["sortY"], by_id["rock"]["sortY"]), (40, 45))
            self.assertEqual((by_id["sign"]["sort_source"], by_id["hero"]["kind"], by_id["shadow"]["band"]),
                             ("explicit", "actor", "background"))

    def test_draw_order_function_breaks_ties_by_x_then_id(self):
        def item(ident, x, sort_y, band="world"):
            return COMPOSE.Placed(group="props", id=ident, image_path=Path("p.png"),
                                  image_sha256="0" * 64, source_size=(1, 1), sprite=Image.new("RGBA", (1, 1)),
                                  left=0, top=0, width=1, height=1, world=(x, sort_y), anchor_px=(0, 0),
                                  anchor_source="px", anchor_canvas=(x, sort_y), sort_y=sort_y,
                                  sort_source="ground-line", layer="props", band=band, resampler="lanczos",
                                  footprint=None)
        items = [item("b", 10, 5), item("a", 10, 5), item("c", 2, 5), item("top", 0, 1),
                 item("fg", 0, -9, "foreground")]
        self.assertEqual([entry.id for entry in COMPOSE.draw_order(items)], ["top", "c", "a", "b", "fg"])
        self.assertEqual([entry.id for entry in COMPOSE.draw_order(items, "raw-y")], ["top", "b", "a", "c", "fg"])


class RoundingAndAnchorErrorTests(unittest.TestCase):
    def test_repro_02_half_up_rounding_moves_one_pixel_per_world_pixel(self):
        """repro_02 (MAP-21): an odd-width bottom-centre prop moved 1 world px moves 1 canvas px every time."""
        lefts = [COMPOSE.placement_xy({"x": x, "y": 50}, 5, 8)[0] for x in range(10, 16)]
        self.assertEqual(lefts, [8, 9, 10, 11, 12, 13])
        self.assertEqual([COMPOSE.forge_core.round_half_up(value) for value in (2.5, -2.5, 3.5, -0.5)], [3, -2, 4, 0])
        self.assertFalse(hasattr(COMPOSE, "_local_round_half_up"))  # D30: forge_core's rule, no private copy

    def test_anchor_world_error_reports_the_rounding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (5, 8), (255, 255, 255, 255)).save(root / "odd.png")
            entry = COMPOSE.paste_prop(Image.new("RGBA", (40, 40)), {"image": "odd.png", "x": 10, "y": 20}, [root])
            self.assertEqual((entry["left"], entry["anchor_canvas"], entry["anchor_world_error"]),
                             (8, [10.5, 20.0], [0.5, 0.0]))
            entry = COMPOSE.paste_prop(Image.new("RGBA", (40, 40)), {"image": "odd.png", "x": 10.5, "y": 20}, [root])
            self.assertEqual(entry["anchor_world_error"], [0.0, 0.0])


class ManifestAnchorTests(unittest.TestCase):
    """B12-T2: anchors from the prop-pack manifest (MAP-02), with the legacy --anchor px switch."""

    def scene(self, root, placement):
        Image.new("RGBA", (120, 120), (200, 200, 200, 255)).save(root / "base.png")
        manifest, _ = write_pack(root, {"tree": (padded_tree(), (20, 52), {
            "footprint": {"shape": "ellipse", "width": 16, "depth": 6, "offset": [0, -3]}, "solid": True})})
        (root / "placements.json").write_text(json.dumps({"props": [placement]}), encoding="utf-8")
        return manifest

    def lowest_art_row(self, path, column=60):
        pixels = rgba(path)[:, column, :3].astype(int)
        return int(np.flatnonzero(np.abs(pixels - 200).sum(axis=1) > 30).max())

    def test_manifest_anchor_puts_the_art_base_on_the_ground_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, {"id": "tree", "image": "pack/tree/prop.png", "x": 60, "y": 100})
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png", "--report", root / "report.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(self.lowest_art_row(root / "out.png"), 99)
            entry = json.loads((root / "report.json").read_text(encoding="utf-8"))["pasted"][0]
            self.assertEqual((entry["anchor_source"], entry["anchorPx"], entry["anchor_world_error"]),
                             ("manifest", [20, 52], [0, 0]))
            self.assertEqual(entry["footprint"]["cy"], 97)

    def test_legacy_anchor_px_keeps_the_bottom_centre(self):
        """The cfed170 rule leaves a padded prop floating by its padding (8 px here)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, {"id": "tree", "image": "pack/tree/prop.png", "x": 60, "y": 100})
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png", "--report", root / "report.json", "--anchor", "px")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(self.lowest_art_row(root / "out.png"), 91)
            entry = json.loads((root / "report.json").read_text(encoding="utf-8"))["pasted"][0]
            self.assertEqual(entry["anchor_source"], "box:center-bottom")

    def test_explicit_prop_pack_and_placement_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.scene(root, {})
            (root / "elsewhere").mkdir()
            padded_tree().save(root / "elsewhere" / "tree.png")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["accepted"][0]["image"] = "elsewhere/tree.png"
            (root / "other-pack.json").write_text(json.dumps(data), encoding="utf-8")
            cases = [({"image": "elsewhere/tree.png", "x": 60, "y": 100}, [], "box:center-bottom"),
                     ({"image": "elsewhere/tree.png", "x": 60, "y": 100}, ["--prop-pack", root / "other-pack.json"],
                      "manifest"),
                     ({"image": "elsewhere/tree.png", "x": 60, "y": 100, "anchorPx": [20, 60]},
                      ["--prop-pack", root / "other-pack.json"], "px")]
            for index, (placement, extra, expected) in enumerate(cases):
                with self.subTest(case=index):
                    (root / "placements.json").write_text(json.dumps({"props": [placement]}), encoding="utf-8")
                    run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                                  "--output", root / f"out{index}.png", "--report", root / f"r{index}.json", *extra)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    entry = json.loads((root / f"r{index}.json").read_text(encoding="utf-8"))["pasted"][0]
                    self.assertEqual(entry["anchor_source"], expected)

    def test_changed_prop_image_refuses_the_stale_manifest_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, {"id": "tree", "image": "pack/tree/prop.png", "x": 60, "y": 100})
            Image.new("RGBA", (40, 60), (1, 2, 3, 255)).save(root / "pack" / "tree" / "prop.png")
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png")
            self.assertEqual(run.returncode, 1)
            self.assertIn("sha256 differs", run.stderr)
            self.assertFalse((root / "out.png").exists())

    def test_v1_manifest_without_anchor_warns_and_uses_the_bottom_centre(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (120, 120), (200, 200, 200, 255)).save(root / "base.png")
            write_pack(root, {"tree": (padded_tree(), (20, 52), {})}, schema=False)
            (root / "placements.json").write_text(json.dumps(
                {"props": [{"image": "pack/tree/prop.png", "x": 60, "y": 100}]}), encoding="utf-8")
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png", "--report", root / "report.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("without anchor_px", run.stderr)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["pasted"][0]["anchor_source"], "box:center-bottom")
            self.assertTrue(any("without anchor_px" in warning for warning in report["warnings"]))

    def test_explicit_anchor_manifest_needs_a_manifest_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (10, 10)).save(root / "lone.png")
            with self.assertRaisesRegex(ValueError, "prop-pack manifest"):
                COMPOSE.prepare_placement({"image": "lone.png", "anchor": "manifest"}, [root],
                                          packs=COMPOSE.PropPackIndex())
            with self.assertRaisesRegex(ValueError, "needs anchorPx"):
                COMPOSE.prepare_placement({"image": "lone.png", "anchor": "px"}, [root])

    def test_manifest_footprint_is_scaled_once_by_the_instance_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, {})
            packs = COMPOSE.PropPackIndex([root / "pack" / "prop-pack.json"])
            item = COMPOSE.prepare_placement({"image": "pack/tree/prop.png", "x": 60, "y": 100, "scale": 2}, [root],
                                             packs=packs)
            self.assertEqual((item.width, item.height, item.anchor_source), (80, 120, "manifest"))
            self.assertEqual({key: item.footprint[key] for key in ("cx", "cy", "rx", "ry")},
                             {"cx": 60.0, "cy": 94.0, "rx": 16.0, "ry": 6.0})


class AliasAndOutputTests(unittest.TestCase):
    """MAP-22 (repro_03) and the standard CLI tests: --help, refusing an existing output, strict QC."""

    def scene(self, root):
        Image.new("RGBA", (64, 64), (90, 90, 90, 255)).save(root / "base.png")
        Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(root / "p.png")
        (root / "placements.json").write_text(json.dumps({"props": [{"image": "p.png", "x": 32, "y": 40}]}),
                                              encoding="utf-8")
        return {path: path.read_bytes() for path in (root / "base.png", root / "p.png", root / "placements.json")}

    def test_help_works_under_cp1252(self):
        assert_cli_help(SKILL, "compose_layered_preview")

    def test_repro_03_outputs_never_overwrite_inputs(self):
        """repro_03 (MAP-22): --output on the base or --report on the placements exits 1 and changes nothing."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            originals = self.scene(root)
            os.link(root / "p.png", root / "linked.png")
            cases = [("--output", root / "base.png", ()), ("--report", root / "placements.json", ()),
                     ("--debug-overlay", root / "p.png", ()), ("--audit-out", root / "linked.png", ()),
                     ("--plate-pan", root / "BASE.PNG" if os.name == "nt" else root / "base.png", ())]
            for role, target, _ in cases:
                with self.subTest(role=role):
                    arguments = ["--base", root / "base.png", "--placements", root / "placements.json"]
                    arguments += ["--output", root / "fresh.png"] if role != "--output" else []
                    run = compose(*arguments, role, target)
                    self.assertEqual(run.returncode, 1, run.stdout)
                    self.assertIn("aliases the input", run.stderr)
                    self.assertFalse((root / "fresh.png").exists())
                    for path, data in originals.items():
                        self.assertEqual(path.read_bytes(), data)

    def test_outputs_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "same.png", "--debug-overlay", root / "same.png")
            self.assertEqual(run.returncode, 1)
            self.assertIn("name the same file", run.stderr)
            self.assertFalse((root / "same.png").exists())

    def test_refuses_an_existing_output_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            (root / "report.json").write_text("keep", encoding="utf-8")
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png", "--report", root / "report.json")
            self.assertEqual(run.returncode, 1)
            self.assertTrue(run.stderr.startswith("error: refusing to replace existing output --report"), run.stderr)
            self.assertEqual((root / "report.json").read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "out.png").exists())

    def test_strict_audit_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            (root / "placements.json").write_text(json.dumps(
                {"props": [{"id": "lost", "image": "p.png", "x": 500, "y": 40}]}), encoding="utf-8")
            outputs = [root / "out" / name for name in ("preview.png", "report.json", "overlay.png", "audit.json",
                                                        "pan.png")]
            run = compose("--base", root / "base.png", "--placements", root / "placements.json", "--strict",
                          "--output", outputs[0], "--report", outputs[1], "--debug-overlay", outputs[2],
                          "--audit-out", outputs[3], "--plate-pan", outputs[4], "--pan-viewport", "32x18")
            self.assertEqual(run.returncode, 1)
            self.assertIn("strict audit failed (bounds)", run.stderr)
            self.assertFalse(any(path.exists() for path in outputs))
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", outputs[0], "--audit-out", outputs[3])
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["audit_status"], "fail")

    def test_a_failed_later_publish_removes_the_earlier_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            real = COMPOSE.forge_core.publish_file_no_replace
            calls = []

            def flaky(source, destination):
                calls.append(destination)
                if len(calls) == 2:
                    raise OSError("disk full")
                real(source, destination)

            with mock.patch.object(COMPOSE.forge_core, "publish_file_no_replace", flaky):
                status = run_main(["--base", root / "base.png", "--placements", root / "placements.json",
                                   "--output", root / "out.png", "--report", root / "report.json"])
            self.assertEqual(status, 1)
            self.assertEqual(len(calls), 2)
            self.assertFalse((root / "out.png").exists())
            self.assertFalse((root / "report.json").exists())

    def test_invalid_numbers_are_errors_not_tracebacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            for bad in ({"x": "12"}, {"w": 4, "scale": 2}, {"sortY": float("inf")}, {"resampler": "cubic"}):
                with self.subTest(bad=bad):
                    (root / "placements.json").write_text(json.dumps({"props": [{"image": "p.png", **bad}]}),
                                                          encoding="utf-8")
                    run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                                  "--output", root / "out.png")
                    self.assertEqual(run.returncode, 1)
                    self.assertTrue(run.stderr.startswith("error: "), run.stderr)
                    self.assertNotIn("Traceback", run.stderr)


class ScaleAndWarningTests(unittest.TestCase):
    def test_scale_two_with_nearest_equals_the_upscaled_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rng = np.random.default_rng(3)
            Image.fromarray(rng.integers(0, 255, (30, 40, 4), np.uint8) | np.uint8(0)).save(root / "base.png")
            sprite = rng.integers(0, 255, (6, 4, 4), np.uint8)
            sprite[..., 3] = np.where(rng.random((6, 4)) > 0.3, 255, 0)
            Image.fromarray(sprite).save(root / "s.png")
            data = {"props": [{"image": "s.png", "x": 10, "y": 12}, {"image": "s.png", "x": 13, "y": 14},
                              {"image": "s.png", "x": 30, "y": 29, "anchorPx": [1, 5], "w": 8, "h": 12}]}
            (root / "p.json").write_text(json.dumps(data), encoding="utf-8")
            common = ["--base", root / "base.png", "--placements", root / "p.json", "--resampler", "nearest"]
            self.assertEqual(compose(*common, "--output", root / "x1.png").returncode, 0)
            run = compose(*common, "--output", root / "x2.png", "--scale", "2", "--report", root / "r2.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            upscaled = np.asarray(Image.open(root / "x1.png").resize((80, 60), Image.Resampling.NEAREST))
            np.testing.assert_array_equal(rgba(root / "x2.png"), upscaled)
            report = json.loads((root / "r2.json").read_text(encoding="utf-8"))
            self.assertEqual((report["scale"], report["canvas_size"]), (2, [80, 60]))
            self.assertEqual(report["pasted"][0]["anchor_world"], [10, 12])
            run = compose(*common, "--output", root / "x3.png", "--scale", "1.5")
            self.assertEqual(run.returncode, 1)
            self.assertIn("whole number", run.stderr)

    def test_unknown_enums_and_unread_lists_warn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGBA", (64, 64), (90, 90, 90, 255)).save(root / "base.png")
            write_pack(root, {"tree": (padded_tree(), (20, 52), {"occlusion_class": "huge"})})
            data = {"schema": "generate2dmap.placements.v2",
                    "props": [{"id": "a", "image": "pack/tree/prop.png", "x": 20, "y": 60, "anchor": "manifest",
                               "layer": "foregound"},
                              {"id": "b", "image": "pack/tree/prop.png", "x": 40, "y": 60, "anchor": "bottom-centre",
                               "anchorPx": [20, 52], "layer": "props",
                               "footprint": {"shape": "circle", "width": 4, "depth": 4}}],
                    "actor": [{"id": "lost-hero", "image": "pack/tree/prop.png"}]}
            (root / "placements.json").write_text(json.dumps(data), encoding="utf-8")
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "out.png", "--report", root / "report.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            warnings = json.loads((root / "report.json").read_text(encoding="utf-8"))["warnings"]
            for fragment in ("layer 'foregound'", "anchor 'bottom-centre' is unknown", "key 'actor' is not read",
                             "occlusion_class 'huge'", "footprint shape 'circle'"):
                with self.subTest(fragment=fragment):
                    self.assertTrue(any(fragment in warning for warning in warnings), warnings)
                    self.assertIn(fragment, run.stderr)
            self.assertEqual(json.loads(run.stdout)["warnings"], len(warnings))


class OverlayAndAuditTests(unittest.TestCase):
    """B12-T3: debug overlay geometry, the placement audit, overlaps and plate pan."""

    def scene(self, root, actor_xy=(100, 140), extra_props=()):
        Image.new("RGBA", (240, 180), (100, 100, 100, 255)).save(root / "base.png")
        write_pack(root, {"tree": (padded_tree(), (20, 52), {
            "footprint": {"shape": "ellipse", "width": 16, "depth": 8, "offset": [0, -2]}, "solid": True})})
        Image.new("RGBA", (10, 20), (220, 30, 30, 255)).save(root / "hero.png")
        mask = Image.new("RGBA", (240, 180))
        mask.paste((255, 255, 255, 255), (180, 10, 200, 30))
        mask.save(root / "mask.png")
        placements = {"schema": "generate2dmap.placements.v2",
                      "props": [{"id": "t1", "image": "pack/tree/prop.png", "x": 60, "y": 100, "anchor": "manifest",
                                 "layer": "props"},
                                {"id": "t2", "image": "pack/tree/prop.png", "x": 68, "y": 102, "anchor": "manifest",
                                 "layer": "props"}, *extra_props],
                      "actors": [{"id": "hero", "image": "hero.png", "x": actor_xy[0], "y": actor_xy[1],
                                  "anchor": "px", "anchorPx": [5, 20], "layer": "actors"}]}
        (root / "placements.json").write_text(json.dumps(placements), encoding="utf-8")
        bundle = {"schema": "generate2dmap.map_bundle.v2", "world": {"width": 240, "height": 180, "unit": "px"},
                  "layers": [{"name": "ground", "kind": "image", "image": "base.png"}],
                  "collision": {"actorRadius": 4, "ySquash": 0.5,
                                "walkRegions": [{"polygon": [[20, 40], [220, 40], [220, 170], [20, 170]],
                                                 "holes": [[[150, 120], [180, 120], [180, 150], [150, 150]]]}],
                                "solids": [{"shape": "rect", "id": "wall", "x": 30, "y": 50, "w": 20, "h": 20}]},
                  "portals": [{"id": "east", "rect": [200, 60, 20, 30], "to": "town:west", "activation": "intent",
                               "travelDirection": [1, 0], "radius": 8}],
                  "spawns": [{"id": "start", "x": 40, "y": 150, "facing": 0}],
                  "anchors": {"well": {"point": [120, 60], "approach": [120, 75]}},
                  "interactions": [{"id": "sign", "x": 60, "y": 130, "reach": 8}]}
        (root / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        return placements, bundle

    def run_scene(self, root, *extra):
        return compose("--base", root / "base.png", "--placements", root / "placements.json",
                       "--output", root / "out" / "preview.png", "--bundle", root / "bundle.json", *extra)

    def test_fixtures_follow_their_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placements, bundle = self.scene(root)
            assert_valid_contract(placements, "map", "placements_v2", skill=SKILL)
            assert_valid_contract(bundle, "map", "map_bundle_v2", skill=SKILL)
            assert_valid_contract(json.loads((root / "pack" / "prop-pack.json").read_text(encoding="utf-8")),
                                  "map", "prop_pack_v2", skill=SKILL)

    def test_debug_overlay_draws_walk_regions_collision_exits_and_masks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            run = self.run_scene(root, "--debug-overlay", root / "out" / "overlay.png", "--mask", root / "mask.png")
            self.assertEqual(run.returncode, 0, run.stderr)
            preview = rgba(root / "out" / "preview.png").astype(int)
            overlay = rgba(root / "out" / "overlay.png").astype(int)
            self.assertGreater(overlay.shape[1], preview.shape[1])
            self.assertEqual(overlay.shape[0], max(preview.shape[0], overlay.shape[0]))
            region = overlay[:180, :240]
            walk, hole, wall, masked = region[160, 100], region[135, 165], region[60, 40], region[20, 190]
            self.assertGreater(walk[1], walk[0])                      # green walk-area tint
            self.assertLess(hole[:3].sum(), walk[:3].sum())          # holes are darker
            self.assertGreater(wall[0], walk[0] + 30)                 # the red solid blends over the walk area
            self.assertLess(wall[1], walk[1])
            self.assertGreater(masked[2], preview[20, 190, 2] + 20)   # blue mask tint
            np.testing.assert_array_equal(region[175, 5], preview[175, 5])  # untouched outside the geometry
            portal_edge = region[60, 210]
            self.assertGreater(portal_edge[0], portal_edge[2] + 60)   # orange exit outline

    def test_audit_reports_overlap_feet_bounds_and_occlusion(self):
        """Acceptance: an overlap is reported (the two tree footprints)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, extra_props=[{"id": "far", "image": "hero.png", "x": 400, "y": 50, "anchor": "px",
                                           "anchorPx": [5, 20], "layer": "props"},
                                          {"id": "edge", "image": "hero.png", "x": 2, "y": 60, "anchor": "px",
                                           "anchorPx": [5, 20], "layer": "props"}])
            run = self.run_scene(root, "--audit-out", root / "out" / "audit.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            audit = json.loads((root / "out" / "audit.json").read_text(encoding="utf-8"))
            assert_valid_contract(audit, "common", "qaEnvelope", skill=SKILL)
            checks = {check["id"]: check for check in audit["checks"]}
            self.assertEqual(checks["footprint_overlaps"]["status"], "warn")
            self.assertEqual([(pair["a"], pair["b"]) for pair in checks["footprint_overlaps"]["value"]["pairs"]],
                             [("t1", "t2")])
            self.assertGreater(checks["footprint_overlaps"]["value"]["pairs"][0]["area_px"], 0)
            self.assertEqual(checks["actor_feet_valid"]["status"], "pass")
            self.assertEqual(checks["bounds"]["value"]["off_canvas"], ["far"])
            self.assertEqual(checks["bounds"]["value"]["clipped"], ["edge"])
            self.assertEqual(checks["bounds"]["status"], "fail")
            self.assertEqual(checks["feet_in_walk_area"]["value"]["outside"], ["far", "edge"])
            self.assertEqual(audit["status"], "fail")
            rows = {row["id"]: row for row in audit["placements"]}
            self.assertEqual(rows["t1"]["occluded_by"], ["t2"])
            self.assertGreater(rows["t1"]["occluded_fraction"], 0)
            self.assertEqual(rows["t2"]["occluded_fraction"], 0)
            self.assertEqual({ref["path"] for ref in audit["outputs"]}, {"preview.png"})
            self.assertEqual(audit["outputs"][0]["sha256"], sha256(root / "out" / "preview.png"))
            self.assertIn("../pack/tree/prop.png", {ref["path"] for ref in audit["inputs"]})

    def test_actor_inside_a_solid_or_hole_fails_and_hidden_actor_warns(self):
        """With --bundle, actor feet are judged on the bundle's D2 blocking set (D2, D4), as map_nav does:
        a solid and a hole fail. The trees are placement footprints, which the bundle does not know, so
        an actor in their trunks stays valid and is reported by actor_feet_off_placement_footprints."""
        cases = {"in the hole": ((165, 135), []), "against the wall": ((52, 60), ["wall"])}
        for name, (xy, blockers) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.scene(root, actor_xy=xy)
                run = self.run_scene(root, "--audit-out", root / "out" / "audit.json")
                self.assertEqual(run.returncode, 0, run.stderr)
                audit = json.loads((root / "out" / "audit.json").read_text(encoding="utf-8"))
                checks = {check["id"]: check for check in audit["checks"]}
                self.assertEqual(checks["actor_feet_valid"]["status"], "fail")
                self.assertEqual(checks["actor_feet_valid"]["value"]["invalid"], ["hero"])
                row = next(row for row in audit["placements"] if row["id"] == "hero")
                self.assertEqual(row["blocked_by"], blockers)
                self.assertEqual(checks["actor_feet_valid"]["value"]["model"], "bundle")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, actor_xy=(62, 99))  # inside the tree trunks t1 and t2
            run = self.run_scene(root, "--audit-out", root / "out" / "audit.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            audit = json.loads((root / "out" / "audit.json").read_text(encoding="utf-8"))
            checks = {check["id"]: check for check in audit["checks"]}
            self.assertEqual(checks["actor_feet_valid"]["status"], "pass")
            self.assertEqual(checks["actor_feet_off_placement_footprints"]["status"], "warn")
            self.assertEqual(checks["actor_feet_off_placement_footprints"]["value"]["actors"], ["hero"])
            row = next(row for row in audit["placements"] if row["id"] == "hero")
            self.assertEqual((row["blocked_by"], row["in_placement_footprints"]), ([], ["t1", "t2"]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, actor_xy=(64, 90))
            run = self.run_scene(root, "--audit-out", root / "out" / "audit.json")
            audit = json.loads((root / "out" / "audit.json").read_text(encoding="utf-8"))
            checks = {check["id"]: check for check in audit["checks"]}
            self.assertEqual(checks["actors_visible"]["value"]["hidden"], ["hero"])

    def test_bundle_world_scale_material_map_and_unused_geometry_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            placements, bundle = self.scene(root)
            materials = Image.new("RGB", (120, 90), (0, 0, 0))
            materials.paste((255, 0, 0), (10, 10, 30, 30))
            materials.save(root / "materials.png")
            palette = Image.new("P", (120, 90), 0)
            palette.putpalette([0, 0, 0, 9, 9, 9])
            palette.paste(1, (90, 60, 110, 80))
            palette.save(root / "indexed.png")
            bundle["world"] = {"width": 120, "height": 90, "unit": "px"}
            bundle["collision"]["solids"] = [{"shape": "rect", "id": "wall", "x": 15, "y": 25, "w": 10, "h": 10}]
            bundle["material_map"] = {"image": "materials.png", "materials": {
                "pond": {"class": "liquid", "color": "#ff0000"}, "lava": {"class": "magma", "color": "#00ff00"}}}
            assert_valid_contract({**bundle, "material_map": {"image": "materials.png", "materials": {
                "pond": {"class": "liquid", "color": "#ff0000"}}}}, "map", "map_bundle_v2", skill=SKILL)
            (root / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            run = self.run_scene(root, "--debug-overlay", root / "out" / "overlay.png", "--report",
                                 root / "out" / "report.json")
            self.assertEqual(run.returncode, 0, run.stderr)
            overlay = rgba(root / "out" / "overlay.png").astype(int)
            # The wall spans world 15..25, canvas 30..50 after the 2x world-to-base scale.
            self.assertGreater(overlay[60, 40][0], overlay[160, 100][0] + 30)
            pond = overlay[40, 40]                                            # world (10..30)*2: liquid tint
            self.assertGreater(pond[2], pond[0])
            self.assertIn("class 'magma'", run.stderr)
            bundle["material_map"] = {"image": "indexed.png", "materials": {"tar": {"class": "hazard", "index": 1}}}
            (root / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            run = self.run_scene(root, "--debug-overlay", root / "out" / "indexed.png", "--output",
                                 root / "out" / "preview-2.png")
            self.assertEqual(run.returncode, 0, run.stderr)
            tar = rgba(root / "out" / "indexed.png").astype(int)[140, 200]
            self.assertGreater(tar[0], tar[2] + 30)                           # orange hazard tint
            run = self.run_scene(root, "--output", root / "out" / "plain.png")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("--bundle, --stage and --mask are read only with", run.stderr)

    def test_stage_ground_polygons_are_the_walk_area_without_a_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root, actor_xy=(30, 30))
            stage = {"schema": "generate2dmap.stage.v1", "sourceSize": [240, 180], "fit": "cover",
                     "reviewedAspectRange": [1.3, 2.2],
                     "groundPolygons": [[[0.25, 0.5], [0.75, 0.5], [0.75, 0.95], [0.25, 0.95]]],
                     "slots": {"hero": [[0.4, 0.8]], "enemy": [[0.6, 0.8]]},
                     "protectedRegions": [{"id": "statue", "box": [0.45, 0.1, 0.55, 0.3]}],
                     "approachPoints": [[0.5, 0.6]], "playableBand": [0.5, 0.95]}
            assert_valid_contract(stage, "map", "stage_v1", skill=SKILL)
            (root / "stage.json").write_text(json.dumps(stage), encoding="utf-8")
            run = compose("--base", root / "base.png", "--placements", root / "placements.json",
                          "--output", root / "preview.png", "--stage", root / "stage.json",
                          "--audit-out", root / "audit.json", "--debug-overlay", root / "overlay.png")
            self.assertEqual(run.returncode, 0, run.stderr)
            audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
            checks = {check["id"]: check for check in audit["checks"]}
            self.assertEqual(audit["walk_area"], "stage.json groundPolygons")
            self.assertEqual(checks["actor_feet_valid"]["value"]["invalid"], ["hero"])
            overlay = rgba(root / "overlay.png").astype(int)
            protected = overlay[45, 120]
            self.assertNotEqual(tuple(protected[:3]), (100, 100, 100))

    def test_plate_pan_is_deterministic_and_reports_its_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            for name in ("a", "b"):
                run = self.run_scene(root, "--plate-pan", root / f"pan-{name}.png", "--pan-viewport", "64x36",
                                     "--pan-frames", "3", "--report", root / f"report-{name}.json",
                                     "--output", root / f"preview-{name}.png")
                self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((root / "pan-a.png").read_bytes(), (root / "pan-b.png").read_bytes())
            with Image.open(root / "pan-a.png") as sheet:
                self.assertEqual(sheet.size, (64, 3 * 36 + 2 * COMPOSE.PAN_GAP_PX))
            report = json.loads((root / "report-a.json").read_text(encoding="utf-8"))
            pan = report["plate_pan"]
            self.assertEqual((pan["file"], pan["viewport"], pan["zoom"]), ("pan-a.png", [64, 36], 1.12))
            windows = [frame["window"] for frame in pan["frames"]]
            self.assertEqual(windows[0][0], 0)
            self.assertAlmostEqual(windows[-1][2], 240, places=6)
            self.assertAlmostEqual(windows[0][2] - windows[0][0], 240 / 1.12, places=5)
            self.assertAlmostEqual((windows[0][2] - windows[0][0]) / (windows[0][3] - windows[0][1]), 64 / 36,
                                   places=5)

    def test_plate_pan_function_follows_the_cover_window_rule(self):
        plate = Image.fromarray(np.tile(np.arange(200, dtype=np.uint8)[None, :, None], (100, 1, 4)))
        sheet, windows = COMPOSE.plate_pan(plate, (50, 50), zoom=1.25, frames=2, focus_v=0.0, resampler="nearest")
        self.assertEqual([window["window"] for window in windows], [[0, 0, 80, 80], [120, 0, 200, 80]])
        frames = np.asarray(sheet)
        self.assertEqual((int(frames[0, 0, 0]), int(frames[50 + COMPOSE.PAN_GAP_PX, -1, 0])), (0, 199))

    def test_report_validates_against_the_requested_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.scene(root)
            run = self.run_scene(root, "--report", root / "out" / "report.json", "--audit-out",
                                 root / "out" / "audit.json", "--plate-pan", root / "out" / "pan.png",
                                 "--pan-viewport", "32x18")
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((root / "out" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(requested_contract_errors(report), [])
            self.assertEqual((report["base"], report["placements"], report["output"]),
                             ("../base.png", "../placements.json", "preview.png"))
            self.assertEqual(report["prop_packs"][0]["path"], "../pack/prop-pack.json")
            self.assertTrue(all(not item["image"].startswith(("/", "C:", "D:")) for item in report["pasted"]))
            broken = json.loads(json.dumps(report))
            broken["pasted"][0]["anchor_source"] = "feet"
            self.assertTrue(requested_contract_errors(broken))
            summary = json.loads(run.stdout)
            self.assertEqual(Path(summary["report"]), (root / "out" / "report.json").resolve())
            self.assertTrue(run.stdout.isascii())



class IntegrationDecisionTests(unittest.TestCase):
    """Phase 3 decisions: footprint basis (D7), actor feet through forge_nav (D4, D33), v1 packs through
    extract_prop_pack.read_manifest (D10), flip_x (D6) and BOM-tolerant JSON (D28)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        Image.new("RGBA", (120, 120), (200, 200, 200, 255)).save(self.root / "base.png")
        Image.new("RGBA", (1, 1), (255, 255, 255, 255)).save(self.root / "dot.png")

    def audit(self, placements, *extra, name="audit"):
        (self.root / f"{name}.json").write_text(json.dumps(placements), encoding="utf-8")
        run = compose("--base", self.root / "base.png", "--placements", self.root / f"{name}.json", "--output",
                      self.root / f"{name}.png", "--audit-out", self.root / f"{name}-audit.json", *extra)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads((self.root / f"{name}-audit.json").read_text(encoding="utf-8")), run

    def test_footprint_basis_prop_px_is_the_default_and_never_warned(self):
        """D7: prop_px (B10's value) and its legacy alias image_px scale with the sprite; world_px only with
        --scale; nothing about the canonical value is warned (review B12, blocking item 1)."""
        bases = {}
        for basis in (None, "prop_px", "image_px", "world_px"):
            footprint = {"shape": "ellipse", "width": 8, "depth": 4, "offset": [2, -1]}
            if basis:
                footprint["basis"] = basis
            placement = {"id": "a", "image": "dot.png", "x": 50, "y": 60, "anchor": "px", "anchorPx": [0, 0],
                         "scale": 3, "footprint": footprint}
            item = COMPOSE.prepare_placement(placement, [self.root])
            self.assertEqual(item.warnings, [])
            bases[basis] = {key: item.footprint[key] for key in ("cx", "cy", "rx", "ry")}
        self.assertEqual(bases[None], bases["prop_px"])
        self.assertEqual(bases["prop_px"], bases["image_px"])
        self.assertEqual(bases["prop_px"], {"cx": 56.0, "cy": 57.0, "rx": 12.0, "ry": 6.0})
        self.assertEqual(bases["world_px"], {"cx": 52.0, "cy": 59.0, "rx": 4.0, "ry": 2.0})
        unknown = COMPOSE.prepare_placement({"image": "dot.png", "x": 5, "y": 5, "footprint": {
            "shape": "rect", "width": 2, "depth": 2, "basis": "screen_px"}}, [self.root])
        self.assertTrue(any("basis 'screen_px'" in warning and "read as prop_px" in warning
                            for warning in unknown.warnings))

    def boundary_bundle(self):
        """check_b13.py's probe, scaled down: a walk region with a hole, a rect, an ellipse and a polygon
        solid, actor radius 0."""
        return {"schema": "generate2dmap.map_bundle.v2", "id": "probe",
                "world": {"width": 120, "height": 120, "unit": "px"},
                "layers": [{"name": "ground", "kind": "image", "image": "base.png"}],
                "collision": {"actorRadius": 0, "walkRegions": [{
                    "polygon": [[10, 10], [110, 10], [110, 110], [10, 110]],
                    "holes": [[[70, 70], [90, 70], [90, 90], [70, 90]]]}],
                    "solids": [{"shape": "rect", "x": 20, "y": 20, "w": 20, "h": 20},
                               {"shape": "ellipse", "cx": 60, "cy": 40, "rx": 10, "ry": 5},
                               {"shape": "polygon", "points": [[20, 60], [40, 60], [40, 80], [20, 80]]}]},
                "spawns": [{"id": "s", "x": 15, "y": 15}]}

    def test_actor_feet_agree_with_map_nav_on_boundary_points(self):
        """D4 (review B12, blocking item 2, repro B): with --bundle, an actor's feet are valid exactly when
        map_nav.py query says so, on rect, ellipse and polygon edges, walk-region and hole edges."""
        (self.root / "probe.json").write_text(json.dumps(self.boundary_bundle()), encoding="utf-8")
        points = [(40, 30), (20, 30), (30, 20), (30, 40), (70, 40), (60, 45), (10, 50), (110, 50), (50, 10),
                  (50, 110), (70, 80), (90, 80), (80, 70), (80, 90), (40, 70), (30, 80), (39.99, 30), (40.01, 30)]
        actors = [{"id": f"a{i}", "image": "dot.png", "x": x, "y": y, "anchor": "px", "anchorPx": [0, 0]}
                  for i, (x, y) in enumerate(points)]
        audit, _ = self.audit({"props": [], "actors": actors}, "--bundle", self.root / "probe.json")
        rows = {row["id"]: row for row in audit["placements"]}
        compose_valid = [rows[f"a{i}"]["foot_valid"] for i in range(len(points))]
        nav = run_cli([script_path("generate2dmap", "map_nav"), "query", "--bundle", self.root / "probe.json",
                       *[part for x, y in points for part in ("--point", f"{x},{y}")]])
        self.assertEqual(nav.returncode, 0, nav.stderr)
        self.assertEqual(compose_valid, [point["valid"] for point in json.loads(nav.stdout)["points"]])
        self.assertEqual(compose_valid[:4], [False] * 4)  # closed rect: its edges block (D1)
        checks = {check["id"]: check for check in audit["checks"]}
        self.assertEqual(checks["actor_feet_valid"]["value"]["model"], "bundle")

    def test_actor_on_a_solid_material_pixel_fails_like_map_nav(self):
        """D2 and D4 (review B12, repro A): material classes block in the audit as in map_nav."""
        bundle = self.boundary_bundle()
        materials = np.zeros((120, 120, 4), np.uint8)
        materials[...] = (0, 0, 0, 255)
        materials[100:110, 50:60] = (128, 128, 128, 255)  # rock: solid
        materials[100:110, 70:80] = (0, 0, 255, 255)  # deep water: blocks unless walkable
        Image.fromarray(materials).save(self.root / "materials.png")
        bundle["material_map"] = {"image": "materials.png", "materials": {
            "floor": {"class": "decor", "color": "#000000"}, "rock": {"class": "solid", "color": "#808080"},
            "deep": {"class": "liquid", "color": "#0000ff"}}}
        (self.root / "materials-bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        actors = [{"id": name, "image": "dot.png", "x": x, "y": 104, "anchor": "px", "anchorPx": [0, 0]}
                  for name, x in (("on-rock", 55), ("in-water", 75), ("on-floor", 95))]
        audit, _ = self.audit({"props": [], "actors": actors}, "--bundle", self.root / "materials-bundle.json")
        rows = {row["id"]: row for row in audit["placements"]}
        self.assertEqual({name: rows[name]["foot_valid"] for name in rows},
                         {"on-rock": False, "in-water": False, "on-floor": True})
        self.assertEqual(rows["on-rock"]["blocked_by"], ["material_map"])
        nav = run_cli([script_path("generate2dmap", "map_nav"), "query", "--bundle",
                       self.root / "materials-bundle.json", "--point", "55,104", "--point", "75,104",
                       "--point", "95,104"])
        self.assertEqual([p["valid"] for p in json.loads(nav.stdout)["points"]], [False, False, True])

    def test_without_a_bundle_rect_footprints_are_closed(self):
        """D33: the no-bundle path uses the same closed sets: an actor on a rect footprint's edge is blocked."""
        props = [{"id": "crate", "image": "dot.png", "x": 60, "y": 60, "anchor": "px", "anchorPx": [0, 0],
                  "footprint": {"shape": "rect", "width": 10, "depth": 6, "basis": "world_px"}, "solid": True}]
        actors = [{"id": name, "image": "dot.png", "x": x, "y": 60, "anchor": "px", "anchorPx": [0, 0]}
                  for name, x in (("on-edge", 65), ("outside", 65.5))]
        audit, _ = self.audit({"props": props, "actors": actors})
        rows = {row["id"]: row for row in audit["placements"]}
        self.assertEqual((rows["on-edge"]["foot_valid"], rows["outside"]["foot_valid"]), (False, True))
        self.assertEqual(rows["on-edge"]["blocked_by"], ["crate"])
        checks = {check["id"]: check for check in audit["checks"]}
        self.assertEqual(checks["actor_feet_valid"]["value"]["model"], "canvas")
        self.assertEqual(checks["actor_feet_off_placement_footprints"]["status"], "skipped")

    def test_v1_pack_anchors_at_the_art_bottom_through_read_manifest(self):
        """D10: a cfed170 v1 pack is read with extract_prop_pack.read_manifest, so its padded prop stands on
        the art's bottom edge (the v1 bottom-centre rule left it floating by its 8 px padding)."""
        (self.root / "pack" / "tree").mkdir(parents=True)
        padded_tree().save(self.root / "pack" / "tree" / "prop.png")
        item = {"label": "tree", "image": "tree/prop.png", "source_box": [0, 0, 40, 60],
                "padded_crop_bbox": [0, 0, 40, 60], "crop_bbox": [10, 4, 30, 52], "output_size": [40, 60]}
        (self.root / "pack" / "prop-pack.json").write_text(json.dumps({"accepted": [item], "rejected": []}),
                                                           encoding="utf-8")
        (self.root / "placements.json").write_text(json.dumps(
            {"props": [{"id": "tree", "image": "pack/tree/prop.png", "x": 60, "y": 100}]}), encoding="utf-8")
        run = compose("--base", self.root / "base.png", "--placements", self.root / "placements.json",
                      "--output", self.root / "out.png", "--report", self.root / "report.json")
        self.assertEqual(run.returncode, 0, run.stderr)
        entry = json.loads((self.root / "report.json").read_text(encoding="utf-8"))["pasted"][0]
        self.assertEqual((entry["anchor_source"], entry["anchorPx"]), ("manifest", [20, 52]))
        pixels = rgba(self.root / "out.png")[:, 60, :3].astype(int)
        self.assertEqual(int(np.flatnonzero(np.abs(pixels - 200).sum(axis=1) > 30).max()), 99)
        self.assertIn("a v1 prop pack", run.stderr)

    def test_flip_x_mirrors_the_art_and_its_footprint_around_the_anchor(self):
        """D6: flip_x draws the art mirrored around the anchor x (the anchor still lands on x) and mirrors
        the footprint with it."""
        art = np.zeros((10, 6, 4), np.uint8)
        art[:, :3] = (220, 30, 30, 255)
        art[:, 3:] = (30, 30, 220, 255)
        Image.fromarray(art).save(self.root / "post.png")
        common = {"image": "post.png", "y": 50, "anchor": "px", "anchorPx": [1, 10],
                  "footprint": {"shape": "rect", "width": 2, "depth": 2, "offset": [2, 0]}}
        plain = COMPOSE.prepare_placement({**common, "id": "plain", "x": 30}, [self.root])
        flipped = COMPOSE.prepare_placement({**common, "id": "flipped", "x": 30, "flip_x": True}, [self.root])
        self.assertEqual((plain.left, flipped.left), (29, 25))  # the mirrored anchor 6 - 1 = 5 lands on x 30
        self.assertEqual((plain.anchor_canvas, flipped.anchor_canvas), ((30.0, 50.0), (30.0, 50.0)))
        self.assertEqual((plain.footprint["cx"], flipped.footprint["cx"]), (32.0, 28.0))
        np.testing.assert_array_equal(np.asarray(flipped.sprite), art[:, ::-1])
        entry = COMPOSE.report_entry(flipped, (120, 120))
        self.assertIs(entry["flip_x"], True)
        self.assertNotIn("flip_x", COMPOSE.report_entry(plain, (120, 120)))
        with self.assertRaisesRegex(ValueError, "flip_x must be true or false"):
            COMPOSE.prepare_placement({**common, "x": 30, "flip_x": "yes"}, [self.root])

    def test_placement_footprint_and_solid_follow_the_contract(self):
        """Review B12 (non-blocking 9): the placement footprint and solid of the integrated schema are what
        compose reads; a basis outside D7's enum or a non-boolean solid is refused by the contract."""
        prop = {"id": "crate", "image": "dot.png", "x": 5, "y": 5, "anchor": "px", "anchorPx": [0, 0],
                "layer": "props", "footprint": {"shape": "rect", "width": 4, "depth": 2, "basis": "world_px"},
                "solid": True, "flip_x": True}
        assert_valid_contract({"schema": "generate2dmap.placements.v2", "props": [prop]}, "map", "placements_v2",
                              skill=SKILL)
        for broken in ({**prop, "footprint": {**prop["footprint"], "basis": "screen_px"}}, {**prop, "solid": "yes"},
                       {**prop, "footprint": {"shape": "rect"}}):
            with self.subTest(broken=broken):
                self.assertTrue(contract_errors({"schema": "generate2dmap.placements.v2", "props": [broken]},
                                                "map", "placements_v2", skill=SKILL))
        item = COMPOSE.prepare_placement(prop, [self.root])
        self.assertEqual((item.footprint["solid"], item.footprint["cx"], item.flip_x), (True, 5.0, True))

    def test_json_inputs_may_carry_a_bom(self):
        """D28 (review B12): placements and bundles written with a UTF-8 BOM are read."""
        (self.root / "bom.json").write_text(json.dumps({"props": [{"image": "dot.png", "x": 5, "y": 5}]}),
                                            encoding="utf-8-sig")
        (self.root / "bom-bundle.json").write_text(json.dumps(self.boundary_bundle()), encoding="utf-8-sig")
        run = compose("--base", self.root / "base.png", "--placements", self.root / "bom.json", "--output",
                      self.root / "bom.png", "--bundle", self.root / "bom-bundle.json", "--audit-out",
                      self.root / "bom-audit.json")
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
