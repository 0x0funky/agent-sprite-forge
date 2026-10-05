"""codeart2d autotile_build: Wang-16, three-material Wang (81), blob-47, bevel and flat sets with
an exhaustive seam proof, a non-periodic control, the seam metric, the repetition index and
generate2dmap.tileset.v1 manifests (B20-T1..T4).

All inputs are synthetic: the material specs are written here or are the skill's examples, and the
image textures are generated with numpy. No image model, network or real fixture is involved.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pytest

from forge_testutils import (SKILLS_DIR, assert_cli_help, assert_valid_contract, contract_errors, load_script,
                             run_cli, script_path)


at = load_script("codeart2d", "autotile_build")
SCRIPT = script_path("codeart2d", "autotile_build")
EXAMPLES = SKILLS_DIR / "codeart2d" / "examples"
WATER_GRASS = EXAMPLES / "water-grass.material.json"
DIRT_PATH = EXAMPLES / "dirt-path.material.json"

WATER = {"ramp": ["#1d3d73", "#2c62ab", "#4b98dc", "#a5dcf5"], "walkable": False,
         "texture": {"base": 0, "marks": [{"rows": ["22"]}, {"rows": ["3"]}], "marks_per_tile": [1, 2]},
         "edge": {"colors": [3, 2], "against": ["grass", "dirt"]},
         "shadow": {"colors": ["#43291a", "#6a4a2c", 1], "from": ["grass", "dirt"]}}
GRASS = {"ramp": ["#23502f", "#377a3b", "#5aa344", "#8cc657"],
         "texture": {"base": 2, "marks": [{"rows": ["1.1", ".1."]}, {"rows": ["3"]}], "marks_per_tile": [1, 3]},
         "edge": {"colors": [0], "against": ["water"]}}
DIRT = {"ramp": ["#6a4a2c", "#9c733f", "#c79d62", "#e3c690"],
        "texture": {"base": 2, "marks": [{"rows": ["3", "1"]}, {"rows": ["1"]}], "marks_per_tile": [1, 3]},
        "edge": {"colors": ["#3d2618"], "against": ["grass"]}, "shadow": {"colors": [0, 1], "from": ["grass"]}}
STONE = {"ramp": ["#2b2f3a", "#454b5c", "#646c80", "#8790a6", "#b4bccd"], "walkable": False,
         "texture": {"base": 2, "marks": [{"rows": ["1"]}, {"rows": ["3"]}], "marks_per_tile": [1, 3]},
         "bevel": {"top": [2, 1], "left": [-1], "right": [-1], "bottom": [-2]}}


def write_spec(folder: Path, sets: list[dict], *, tile: int = 16, variants: int = 2, seed: int = 5,
               materials: dict | None = None, name: str = "spec.material.json") -> Path:
    spec = {"schema": "codeart2d.material_spec.v1", "tile_size": tile, "variants": variants, "seed": seed,
            "materials": materials or {"water": WATER, "grass": GRASS, "dirt": DIRT, "stone": STONE},
            "sets": sets}
    path = folder / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


def builder_for(path: Path, index: int = 0, **overrides) -> "at.Builder":
    spec = at.load_spec(path, **overrides)
    return at.BUILDERS[spec.sets[index].kind](at.build_art(spec, spec.sets[index]))


def random_map(builder: "at.Builder", seed: int = 3, size: tuple[int, int] = (30, 20)):
    """Tiles assembled by key and the global render of one seeded random map."""
    rng = np.random.default_rng(seed)
    content = builder.random_content(rng, *size)
    variants = rng.integers(0, builder.art.variants, (1, size[1], size[0]))
    tiles = builder.render_tiles()
    expected, planes = builder.render_global(content, variants)
    actual = builder.assemble(builder.cell_keys(content), variants, tiles)
    return expected, actual, planes, tiles


def cli(*args: str | Path, encoding: str | None = None):
    return run_cli([SCRIPT, *map(str, args)], encoding, timeout=600)


@pytest.fixture(scope="module")
def examples(tmp_path_factory):
    """Both examples built once through the CLI, as the docs run them (strict QC, repetition gate)."""
    root = tmp_path_factory.mktemp("autotile-examples")
    runs = {}
    for name, spec, extra in (("water-grass", WATER_GRASS, ["--kind", "both", "--tile-size", "16", "--variants", "4"]),
                              ("dirt-path", DIRT_PATH, [])):
        out = root / name
        result = cli("--material-spec", spec, *extra, "--output-dir", out, "--preview-map", "24x16",
                     "--max-repetition", "0.35", "--strict-qc")
        runs[name] = (result, out)
    return root, runs


# ----------------------------------------------------------------------------- noise and kernel

def test_periodic_noise_is_bit_identical_when_tiled_and_detuned_noise_is_not():
    noise = at.PeriodicNoise.make(16, 1234)
    local = noise.at(2 * np.arange(16) + 1, 2 * np.arange(16) + 1)
    rows, cols = np.arange(-5, 3 * 16), np.arange(-7, 4 * 16)
    tiled = local[(rows % 16)[:, None], (cols % 16)[None, :]]
    assert np.array_equal(noise.at(2 * rows + 1, 2 * cols + 1), tiled)
    assert np.abs(local).max() == pytest.approx(1.0)
    detuned = noise.detuned()
    detuned_local = detuned.at(2 * np.arange(16) + 1, 2 * np.arange(16) + 1)
    assert not np.allclose(detuned.at(2 * rows + 1, 2 * cols + 1),
                           detuned_local[(rows % 16)[:, None], (cols % 16)[None, :]])


def test_plateau_kernel_is_a_partition_of_unity_with_flat_plateaus():
    tile, plateau = 16, 3
    offsets = np.arange(tile)
    kernel = at.plateau_kernel(offsets, tile, plateau)
    assert np.allclose(kernel + at.plateau_kernel(offsets - tile, tile, plateau), 1.0)
    assert (kernel[:plateau] == 1.0).all() and (kernel[tile - plateau:] == 0.0).all()
    assert (at.plateau_kernel(-np.arange(1, plateau + 1), tile, plateau) == 1.0).all()
    assert (np.diff(kernel) <= 0).all()


# ----------------------------------------------------------------------------- B20-T1 Wang-16

def test_wang16_random_map_equals_global_render(tmp_path):
    """B20-T1: a random 30x20 Wang map assembled from tiles equals the global render (0 px)."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}], variants=3))
    assert len(builder.keys) == 16
    expected, actual, planes, _ = random_map(builder)
    assert expected.shape == (1, 320, 480, 4)
    assert np.array_equal(expected, actual)
    assert set(np.unique(planes.mat)) == {0, 1}
    assert (planes.band >= 0).sum() > 1000   # foam, rims, banks and shadows are drawn


def test_wang16_exhaustive_proof_covers_every_2x2_block(tmp_path):
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}], variants=3))
    proof = builder.prove(builder.render_tiles())
    assert proof["mismatches"] == 0
    assert proof["cases"][0]["configurations"] == 2 ** 9
    assert proof["pixels_compared"] == 2 ** 9 * 4 * 16 * 16 * 3


def test_variants_change_only_the_interior(tmp_path):
    """B20-T1: variants differ (marks) but their 1 px outer rings are identical."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}], variants=4))
    tiles = builder.render_tiles()
    ring = np.ones((16, 16), bool)
    ring[1:-1, 1:-1] = False
    assert (tiles[:, :, ring] == tiles[:1, :, ring]).all()
    full_grass = builder.keys.index((1, 1, 1, 1))
    assert len({tiles[v, full_grass].tobytes() for v in range(4)}) == 4


def test_nonperiodic_control_is_flagged(tmp_path):
    """B20-T1: a copy whose noise is detuned by half a cycle per tile cannot reproduce its own
    global render, while the real set reproduces it exactly."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}]))
    control = at.WangBuilder(builder.art.control())
    expected, actual, _, _ = random_map(control)
    assert (expected != actual).any(-1).sum() > 500
    proof = control.prove(control.render_tiles())
    assert proof["mismatches"] > 0 and proof["examples"]
    blob = builder_for(write_spec(tmp_path, [{"kind": "blob47", "materials": ["grass", "dirt"]}], name="b.json"))
    expected, actual, _, _ = random_map(at.BlobBuilder(blob.art.control()))
    assert (expected != actual).any(-1).sum() > 100


def test_shore_shading_is_exact_with_the_plateau_and_the_probe_field_is_caught(tmp_path):
    """B20-T1 shore shading fix: the north-shore bank and shadow (3 px) are look-ups that cross tile
    edges. With the plateau kernel the tiles are exact; with the plain bilinear blend of the
    2026-10-05 map probe (whose clamped look-up left a 4 px jump between water tiles) the same
    look-ups disagree with the global render, and the proof says so."""
    path = write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}], variants=1)
    builder = builder_for(path)
    assert builder.art.spec.plateau == builder.art.radius == 3
    tiles = builder.render_tiles()
    bank = np.array([0x43, 0x29, 0x1a, 255], np.uint8)
    assert (tiles == bank).all(-1).sum() > 0   # the shadow colours are really drawn
    assert builder.prove(tiles)["mismatches"] == 0
    with pytest.raises(at.SpecError, match="plateau"):
        at.load_spec(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"], "plateau": 1}],
                                name="low.json"))
    spec = at.load_spec(path)
    probe_art = at.build_art(spec, spec.sets[0])
    probe_art.kernel = lambda offsets: np.clip(1.0 - np.abs(np.asarray(offsets, np.float64) + 0.5) / 16, 0.0, 1.0)
    probe = at.WangBuilder(probe_art)
    assert probe.prove(probe.render_tiles())["mismatches"] > 100


def test_wang_collision_and_walkable_follow_the_materials(tmp_path):
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}]))
    blocked = builder.blocked(builder.tile_planes())
    water, grass = builder.keys.index((0, 0, 0, 0)), builder.keys.index((1, 1, 1, 1))
    assert at.collision_rects(blocked[water], 4) == [{"shape": "rect", "x": 0, "y": 0, "w": 16, "h": 16}]
    assert at.collision_rects(blocked[grass], 4) == []
    half = builder.keys.index((1, 1, 0, 0))   # land above, water below
    rects = at.collision_rects(blocked[half], 4)
    assert rects and all(rect["y"] >= 4 for rect in rects)


# ----------------------------------------------------------------------------- B20-T2 blob-47

def test_blob47_has_47_distinct_tiles_and_equals_the_global_render(tmp_path):
    """B20-T2: 47 canonical masks, 47 distinct tiles with 1 px edges, exact against the global render."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "blob47", "materials": ["grass", "dirt"]}]))
    assert len(builder.keys) == 47 and builder.keys[0] == 0 and builder.keys[-1] == 255
    tiles = builder.render_tiles()
    assert len({tiles[0, k].tobytes() for k in range(47)}) == 47
    expected, actual, _, _ = random_map(builder)
    assert np.array_equal(expected, actual)
    proof = builder.prove(tiles)
    assert proof["mismatches"] == 0
    assert {case["configurations"] for case in proof["cases"]} == {256, 1024}
    outline = np.array([0x3d, 0x26, 0x18, 255], np.uint8)
    isolated = tiles[0, builder.keys.index(0)]
    visible = isolated[..., 3] > 0
    assert (isolated == outline).all(-1).sum() > 0 and not visible[0].any() and not visible[:, 0].any()
    full = tiles[0, builder.keys.index(255)]
    assert (full[..., 3] == 255).all()


def test_blob47_wang_data_uses_two_colours(tmp_path):
    """B20-T2: Tiled mixed wangids use colours 1 (under) and 2 (fill), never 0, so the isolated tile
    is not mistaken for an unassigned one."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "blob47", "materials": ["grass", "dirt"]}]))
    wangids = [builder.tile_fields(rank)["wangid"] for rank in range(47)]
    assert all(set(w) <= {1, 2} and len(w) == 8 for w in wangids)
    assert builder.tile_fields(builder.keys.index(0))["wangid"] == [1] * 8
    assert builder.tile_fields(builder.keys.index(255))["wangid"] == [2] * 8
    assert len({tuple(w) for w in wangids}) == 47
    north_east = builder.tile_fields(builder.keys.index(0b111))["wangid"]   # N, NE, E set
    assert north_east == [2, 2, 2, 1, 1, 1, 1, 1]


def test_canonical_blob_drops_diagonals_without_both_sides():
    assert at.canonical_blob(0b00000010) == 0          # NE alone
    assert at.canonical_blob(0b00000111) == 0b111      # N, NE, E
    assert len({int(m) for m in at.canonical_blob(np.arange(256))}) == 47


# ----------------------------------------------------------------------------- B20-T3

def test_three_material_set_has_81_tiles_and_equals_the_global_render(tmp_path):
    """B20-T3: the 81 corner combinations of water, grass and dirt; a road meets water directly."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass", "dirt"]}],
                                     variants=1))
    assert len(builder.keys) == 81
    tiles = builder.render_tiles()
    assert len({tiles[0, k].tobytes() for k in range(81)}) == 81
    expected, actual, planes, _ = random_map(builder)
    assert np.array_equal(expected, actual)
    proof = builder.prove(tiles)
    assert proof["mismatches"] == 0 and proof["cases"][0]["configurations"] == 3 ** 9
    shore = builder.tile_planes().mat[builder.keys.index((0, 0, 2, 2))]   # water above a road
    assert set(np.unique(shore)) == {0, 2}
    assert ((shore[:-1] == 0) & (shore[1:] == 2)).any()


def test_bevel_is_seam_free_and_shades_open_faces(tmp_path):
    """B20-T3 --kind bevel: exact against the global render, no seams inside solid areas, a lit top
    face and a dark bottom face where the block is open."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "bevel", "materials": ["stone"]}]))
    assert len(builder.keys) == 16
    tiles = builder.render_tiles()
    expected, actual, _, _ = random_map(builder)
    assert np.array_equal(expected, actual)
    assert builder.prove(tiles)["mismatches"] == 0
    area = builder.uniform_content(0, 12, 12)
    solid = builder.assemble(builder.cell_keys(area), np.zeros((1, 12, 12), int), tiles)[0, 16:-16, 16:-16]
    assert at.seam_metric(solid, None, 16)["rgb_ratio"] <= 1.0
    isolated = builder.tile_planes().face[builder.keys.index(0)]
    assert (isolated[0] > 0).all() and (isolated[-1] < 0).all() and (isolated[1, 1:-1] == 1).all()
    interior = builder.tile_planes().face[builder.keys.index(0b01010101)]
    assert (interior == 0).all()


def test_flat_set_is_exact_across_every_variant_block(tmp_path):
    builder = builder_for(write_spec(tmp_path, [{"kind": "flat", "materials": ["grass"]}], variants=3))
    proof = builder.prove(builder.render_tiles())
    assert proof["mismatches"] == 0 and proof["cases"][0]["configurations"] == 3 ** 4


def test_repetition_index_drops_with_variants(tmp_path):
    """B20-T3: one variant repeats like wallpaper; four interior-only variants repeat far less."""
    path = write_spec(tmp_path, [{"kind": "flat", "materials": ["grass"]}])
    single = builder_for(path, variants=1)
    tiles = single.render_tiles()
    area = single.assemble(single.cell_keys(single.uniform_content(0, 13, 13)), np.zeros((1, 13, 13), int), tiles)[0]
    assert min(at.repetition_index(area, 16)) > 0.99
    spec = at.load_spec(path, variants=4)
    result = at.evaluate_set(at.FlatBuilder(at.build_art(spec, spec.sets[0])), spec, skip_proof=True, strict=False,
                             max_repetition=None, max_seam_ratio=1.0)
    assert result["metrics"]["repetition_index"] < 0.5


def test_material_texture_is_cut_by_the_mask_and_must_wrap(tmp_path):
    """B20-T3 --material-texture: a one-tile image fills the material exactly; one that does not
    wrap fails texture_wrap and raises the seam metric."""
    yy, xx = (np.mgrid[0:16, 0:16] + 0.5) / 16
    wave = 0.5 + 0.3 * np.sin(2 * np.pi * (xx + 2 * yy)) + 0.2 * np.sin(2 * np.pi * (3 * xx - yy) + 1.0)
    wrapping = np.dstack([60 + 60 * wave, 120 + 70 * wave, 50 + 40 * wave, np.full((16, 16), 255)]).astype(np.uint8)
    ramp = np.linspace(0, 1, 16)[None, :] * np.ones((16, 1))
    broken = np.dstack([60 + 120 * ramp, 120 + 90 * ramp, 50 + 60 * ramp, np.full((16, 16), 255)]).astype(np.uint8)
    Image.fromarray(wrapping).save(tmp_path / "wrap.png")
    Image.fromarray(broken).save(tmp_path / "broken.png")
    grass_plain = dict(GRASS, texture={"base": 2})
    path = write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}],
                      materials={"water": WATER, "grass": grass_plain, "dirt": DIRT})
    builder = builder_for(path, textures={"grass": tmp_path / "wrap.png"})
    tiles = builder.render_tiles()
    full = tiles[0, builder.keys.index((1, 1, 1, 1))]
    assert np.array_equal(full[..., :3], wrapping[..., :3])
    assert at.texture_wrap(wrapping[..., :3])["ratio"] <= at.TEXTURE_WRAP_LIMIT
    assert at.texture_wrap(broken[..., :3])["ratio"] > 3
    _, actual, planes, _ = random_map(builder_for(path, textures={"grass": tmp_path / "broken.png"}))
    assert at.seam_metric(actual[0], planes.mat[0], 16)["rgb_ratio"] > 1.0
    _, actual, planes, _ = random_map(builder)
    assert at.seam_metric(actual[0], planes.mat[0], 16)["rgb_ratio"] <= 1.0
    quantized = builder_for(path, textures={"grass": tmp_path / "wrap.png"}, quantize=True).render_tiles()
    ramp_rgb = {(0x23, 0x50, 0x2f), (0x37, 0x7a, 0x3b), (0x5a, 0xa3, 0x44), (0x8c, 0xc6, 0x57)}
    assert {tuple(p) for p in quantized[0, builder.keys.index((1, 1, 1, 1))][..., :3].reshape(-1, 3)} <= ramp_rgb
    out = tmp_path / "broken-out"
    result = cli("--material-spec", path, "--material-texture", f"grass={tmp_path / 'broken.png'}",
                 "--output-dir", out, "--skip-seam-proof", "--preview-map", "none", "--strict-qc")
    assert result.returncode == 1 and "texture_wrap/grass" in result.stderr and not out.exists()


# ----------------------------------------------------------------------------- spec validation

@pytest.mark.parametrize("change, message", [
    (lambda s: s.update(schema="codeart.material_spec.v0"), "schema"),
    (lambda s: s.update(tile_size=[16, 32]), "square"),
    (lambda s: s.update(tile_size=4), "tile_size"),
    (lambda s: s["sets"].append({"kind": "wang_corner", "materials": ["water", "lava"]}), "unknown material"),
    (lambda s: s["sets"].append({"kind": "blob47", "materials": ["grass", "dirt", "water"]}), "blob47"),
    (lambda s: s["sets"].append({"kind": "blob47", "materials": ["grass", "dirt"], "margin": [1, 3]}), "margin"),
    (lambda s: s["materials"]["grass"].update(ramp=["#zzzzzz"]), "colour"),
    (lambda s: s["materials"]["water"]["edge"].update(colors=[9]), "outside the ramp"),
    (lambda s: s["materials"]["grass"]["texture"].update(marks=[{"rows": ["7"]}]), "ramp positions"),
    (lambda s: s["sets"].append({"kind": "wang_corner", "materials": ["water", "grass"]}), "unique"),
])
def test_spec_errors_are_reported(tmp_path, change, message):
    spec = json.loads(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}]).read_text())
    change(spec)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(at.SpecError, match=message):
        at.load_spec(path)


def test_texture_image_must_be_one_tile(tmp_path):
    Image.new("RGBA", (32, 16), (40, 120, 50, 255)).save(tmp_path / "wide.png")
    path = write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass"]}])
    with pytest.raises(at.SpecError, match="exactly one tile"):
        at.load_spec(path, textures={"grass": tmp_path / "wide.png"})


# ----------------------------------------------------------------------------- B20-T4 CLI

def test_help_works_under_cp1252_and_cp950():
    assert_cli_help("codeart2d", "autotile_build")


def test_examples_exit_zero_with_strict_qc(examples):
    """B20-T4: both examples build with --strict-qc and the 0.35 repetition gate; every set is
    seam-proven and the repetition index is printed."""
    _, runs = examples
    for name, (result, out) in runs.items():
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["status"] == "pass" and summary["failed_checks"] == []
        assert 0 <= summary["repetition_index"] <= 0.35
        assert all(s["seamless_verified"] and s["mismatches"] == 0 and s["pixels_compared"] > 0
                   for s in summary["sets"])
        assert Path(summary["output"]) == Path(out) and Path(summary["metadata"]).name == "codeart-meta.json"
    kinds = {s["kind"] for s in json.loads(runs["dirt-path"][0].stdout)["sets"]}
    assert kinds == {"wang_corner", "blob47", "bevel", "flat"}
    assert [s["id"] for s in json.loads(runs["water-grass"][0].stdout)["sets"]] == \
        ["water-grass-wang16", "grass-dirt-blob47"]


def test_outputs_validate_against_the_contracts(examples):
    """Every document autotile_build writes validates against the vendored codeart2d schemas."""
    _, runs = examples
    for spec in (WATER_GRASS, DIRT_PATH):
        assert_valid_contract(json.loads(spec.read_text(encoding="utf-8")), "codeart", "material_spec_v1",
                              skill="codeart2d")
    for _, out in runs.values():
        meta = json.loads((out / "codeart-meta.json").read_text(encoding="utf-8"))
        assert_valid_contract(meta, "codeart", "codeart_meta_v1", skill="codeart2d")
        assert meta["art_source"] == "code" and meta["qa"]["status"] == "pass"
        qa = json.loads((out / "autotile-qa.json").read_text(encoding="utf-8"))
        assert_valid_contract(qa, "common", "qaEnvelope", skill="codeart2d")
        assert qa["notProven"] and qa["method"] and qa["inputs"][0]["path"].endswith(".material.json")
        assert qa["tool"] == {"name": "codeart2d/autotile_build", "version": "0.4.0"}  # D29
        for ref in qa["outputs"] + meta["outputs"]:
            file = out / ref["path"]
            assert hashlib.sha256(file.read_bytes()).hexdigest() == ref["sha256"]
        manifests = sorted(out.glob("*.tileset.json"))
        listed = {ref["path"] for ref in meta["outputs"]}
        assert {path.name for path in manifests} | {"autotile-qa.json"} <= listed
        assert not {path.name for path in manifests} & {ref["path"] for ref in qa["outputs"]}
        qa_ref = {"path": "autotile-qa.json", "sha256": hashlib.sha256((out / "autotile-qa.json").read_bytes()).hexdigest(),
                  "bytes": (out / "autotile-qa.json").stat().st_size}
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert_valid_contract(manifest, "map", "tileset_v1", skill="codeart2d")
            assert manifest["qa"] == qa_ref  # D5: a fileRef of the QA file, bound to its bytes
            assert_valid_contract(manifest["qa"], "common", "fileRef", skill="codeart2d")
            assert manifest["generator"] == {"name": "codeart2d/autotile_build", "version": "0.4.0"}
            atlas = out / manifest["image"]
            assert hashlib.sha256(atlas.read_bytes()).hexdigest() == manifest["sha256"]
            assert manifest["seamless_verified"] is True and manifest["seam_proof"]["mismatches"] == 0
            assert manifest["seam_proof"]["pixels_compared"] > 0 and "global render" in manifest["seam_proof"]["method"]
            with Image.open(atlas) as image:
                assert image.mode == "RGBA" and image.width == manifest["columns"] * manifest["tile_size"]
                assert -(-manifest["tilecount"] // manifest["columns"]) * manifest["tile_size"] == image.height
            assert [t["index"] for t in manifest["tiles"]] == list(range(manifest["tilecount"]))


def test_outputs_and_examples_validate_against_the_applied_schema_requests(examples):
    """The handoff section 5 requests are in the shared schemas now (S1): the real vendored schemas
    type-check every manifest and both examples, and refuse a three-material blob47 set."""
    _, runs = examples
    for spec in (WATER_GRASS, DIRT_PATH):
        assert_valid_contract(json.loads(spec.read_text(encoding="utf-8")), "codeart", "material_spec_v1",
                              skill="codeart2d")
    bad = json.loads(WATER_GRASS.read_text(encoding="utf-8"))
    bad["sets"].append({"kind": "blob47", "materials": ["grass", "dirt", "water"]})
    assert contract_errors(bad, "codeart", "material_spec_v1", skill="codeart2d")
    for _, out in runs.values():
        for manifest_path in out.glob("*.tileset.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert_valid_contract(manifest, "map", "tileset_v1", skill="codeart2d")
            for tile in manifest["tiles"]:
                assert_valid_contract(tile, "map", "tile", skill="codeart2d")


def test_outputs_are_deterministic(examples):
    root, runs = examples
    again = root / "water-grass-again"
    result = cli("--material-spec", WATER_GRASS, "--kind", "both", "--tile-size", "16", "--variants", "4",
                 "--output-dir", again, "--preview-map", "24x16", "--max-repetition", "0.35", "--strict-qc")
    assert result.returncode == 0, result.stderr
    first = runs["water-grass"][1]
    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(p.name for p in again.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (again / name).read_bytes(), name


def test_refuses_an_existing_output_dir(tmp_path):
    out = tmp_path / "tiles"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    result = cli("--material-spec", WATER_GRASS, "--output-dir", out)
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "already exists" in result.stderr
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_strict_qc_failure_publishes_nothing(tmp_path):
    out = tmp_path / "tiles"
    result = cli("--material-spec", WATER_GRASS, "--kind", "wang", "--output-dir", out, "--preview-map", "none",
                 "--max-repetition", "0", "--strict-qc")
    assert result.returncode == 1 and "repetition_index" in result.stderr and not result.stdout.strip()
    assert list(tmp_path.iterdir()) == []


def test_strict_mode_fails_without_a_seam_proof(tmp_path):
    """B20-T4: --skip-seam-proof cannot pass --strict-qc; without strict the set is published as
    unverified."""
    out = tmp_path / "strict"
    result = cli("--material-spec", WATER_GRASS, "--kind", "wang", "--output-dir", out, "--preview-map", "none",
                 "--skip-seam-proof", "--strict-qc")
    assert result.returncode == 1 and "seam_proof" in result.stderr and not out.exists()
    draft = tmp_path / "draft"
    result = cli("--material-spec", WATER_GRASS, "--kind", "wang", "--output-dir", draft, "--preview-map", "none",
                 "--skip-seam-proof")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "warn" and summary["sets"][0]["seamless_verified"] is False
    manifest = json.loads((draft / "water-grass-wang16.tileset.json").read_text(encoding="utf-8"))
    assert manifest["seamless_verified"] is False
    assert_valid_contract(manifest, "map", "tileset_v1", skill="codeart2d")


def test_bad_arguments_exit_with_an_error_line(tmp_path):
    """D26: a missing file is a runtime error (exit 1); argument errors are argparse usage errors (exit 2)."""
    result = cli("--material-spec", tmp_path / "missing.json", "--output-dir", tmp_path / "o", encoding="cp1252")
    assert result.returncode == 1 and result.stderr.startswith("error: ") and "Traceback" not in result.stderr
    for args in (["--material-spec", str(WATER_GRASS), "--output-dir", str(tmp_path / "o"), "--preview-map", "9"],
                 ["--material-spec", str(WATER_GRASS), "--output-dir", str(tmp_path / "o"), "--kind", "hex"],
                 ["--material-spec", str(WATER_GRASS)]):
        result = cli(*args, encoding="cp1252")
        assert result.returncode == 2 and result.stderr.startswith("usage: autotile_build.py")
        assert "error: " in result.stderr and "Traceback" not in result.stderr
    bom = tmp_path / "bom.material.json"
    bom.write_bytes(b"\xef\xbb\xbf" + WATER_GRASS.read_bytes())
    result = cli("--material-spec", bom, "--kind", "wang", "--output-dir", tmp_path / "bom", "--preview-map", "none")
    assert result.returncode == 0, result.stderr  # D28: a spec saved with a UTF-8 BOM is read
    for index, text in enumerate(["[]", '{"schema": "codeart2d.material_spec.v1", "materials": 3}', "{", "\xff"]):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(text.encode("latin-1"))
        result = cli("--material-spec", path, "--output-dir", tmp_path / f"out-{index}")
        assert result.returncode == 1 and result.stderr.startswith("error: ") and "Traceback" not in result.stderr
        assert "internal error" not in result.stderr, result.stderr
    assert not (tmp_path / "o").exists()


@pytest.mark.perf
def test_three_material_exhaustive_proof_budget(tmp_path):
    """The 81-tile set with 4 variants compares 80.6M pixels; generous budget for shared CI machines."""
    builder = builder_for(write_spec(tmp_path, [{"kind": "wang_corner", "materials": ["water", "grass", "dirt"]}],
                                     variants=4))
    tiles = builder.render_tiles()
    start = time.perf_counter()
    proof = builder.prove(tiles)
    assert proof["mismatches"] == 0 and proof["pixels_compared"] == 3 ** 9 * 4 * 256 * 4
    assert time.perf_counter() - start < 60
