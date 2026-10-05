"""Phase 3 end-to-end smoke: the five pipelines of plan Appendix I, CLI by CLI.

Each pipeline runs the skills' scripts by path in a new project folder the way a user or an agent runs them
from the project root: relative paths, a cp1252 console, every tool reading what the one before it published.
Every step must exit 0 and print one ASCII JSON summary line. When a pipeline is done the whole project is
checked: every JSON document (the inputs written here and everything the tools published) validates against
the skills' vendored schemas, every {path, sha256} reference that resolves names a file with that digest, no
text file holds an absolute path, and no staging folder is left behind.

1. Still: the real fox run sheet -> generate2dsprite process -> scale_frames -> build_animation_clips ->
   export_engine all.
2. Video: a synthetic provider clip keyed on the colour prepare_i2v_input chose -> video2dsprite process
   --matte soft -> register_clip apply -> gait_loop select -> retime -> package png,webm,packed -> verify ->
   validate_animation.
3. Code art: render_pixelspec --build-clips -> export_engine; rig_animate --build-clips -> export_engine;
   fx_build --export-runtime -> fx_verify.mjs.
4. Map: autotile_build -> layout_build --tiles -> map_bundle validate -> map_nav check -> export_tiled (and its
   verify) / export_godot / export_ldtk -> compose_layered_preview --debug-overlay (its feet audit against
   map_nav query) -> build_scene_preview.
5. HD-2D: validate_stage, build_motion_mask, scene_motion build and qa on one plate, and the compose overlay of
   the stage, the mask and actors on the slots.

Deliberately not repeated here: each tool's own options, refusals and failure modes (its module tests) and the
JS/Python collision parity (tests/test_collision_parity.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from forge_testutils import (REPO_ROOT, SKILLS_DIR, contract_errors, load_script, real_fixture, require_ffmpeg,
                             require_node, run_cli, script_path)

pytestmark = pytest.mark.e2e

TEXT_SUFFIXES = {".json", ".jsonl", ".tmj", ".tsx", ".tres", ".tscn", ".ldtk", ".html", ".mjs", ".js", ".txt",
                 ".md", ".svg", ".csv", ".xml"}
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")  # C:\ or C:/ anywhere in the text
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DOMAIN_SKILL = {"sprite": "generate2dsprite", "video": "video2dsprite", "map": "generate2dmap",
                "codeart": "codeart2d", "media": "generate2dmedia", "common": "generate2dmap"}
CLIP_IDS = {"generate2dsprite.animation_clips.v1", "generate2dsprite.animation_clips.v2"}
# Tool reports whose only contract is the common qaEnvelope they embed as "qa" (B07 handoff section 5).
TOOL_REPORTS = {"video2dsprite.loop_report.v1", "video2dsprite.stride_report.v1", "video2dsprite.retime_report.v1",
                "forge-animation-select/v1"}
# Documents that are plain common qaEnvelopes with their own schema id (map.schema.json compose_report_v2,
# B12 handoff section 5).
QA_DOCUMENTS = {"generate2dmap.compose_audit.v1", "generate2dmap.crops_qa.v1"}


def _schema_index() -> dict[str, tuple[str, str]]:
    """schema id -> (domain, $defs name), from each domain's vendored copy in its home skill."""
    index: dict[str, tuple[str, str]] = {}
    for domain, skill in DOMAIN_SKILL.items():
        document = json.loads((SKILLS_DIR / skill / "references" / "schemas" / f"{domain}.schema.json")
                              .read_text(encoding="utf-8"))
        for name, definition in document.get("$defs", {}).items():
            schema = (definition.get("properties") or {}).get("schema") if isinstance(definition, dict) else None
            if isinstance(schema, dict):
                for ident in [schema["const"]] if "const" in schema else schema.get("enum", []):
                    index.setdefault(ident, (domain, name))
    return index


SCHEMA_INDEX = _schema_index()


def _is_qa_envelope(document) -> bool:
    return isinstance(document, dict) and {"status", "method", "notProven", "checks"} <= document.keys()


def contracts_of(path: Path, document) -> list[tuple[str, str, object]] | None:
    """The (domain, $defs name, part) contracts a document must meet, [] for a third-party format, or None when
    the document is not classified (a new kind of output that this test must learn about)."""
    if not isinstance(document, dict):
        return None
    ident = document.get("schema")
    found: list[tuple[str, str, object]] = []
    if ident in CLIP_IDS:  # the builder's output, or a clips manifest it reads (or copied as source-manifest.json)
        found.append(("sprite", "animation_clips_v2" if path.name == "animation-clips.json" else "clips_input",
                      document))
    elif ident in SCHEMA_INDEX:
        found.append((*SCHEMA_INDEX[ident], document))
    elif ident in TOOL_REPORTS:
        found.append(("common", "qaEnvelope", document.get("qa")))
    elif ident in QA_DOCUMENTS:
        found.append(("common", "qaEnvelope", document))
    elif ident is not None:
        return None
    elif document.get("schemaVersion") in ("2.0", "3.0") and "frameCount" in document:
        found.append(("video", "animation_v3", document))  # engine_export package manifest
    elif path.name == "pipeline-meta.json" and {"extract", "matte"} <= document.keys():
        found.append(("video", "matte_report_v1", document["matte"]))  # video2dsprite process (B05 handoff)
    elif {"props", "actors"} & document.keys() and path.parent.name == "art":
        found.append(("map", "placements_v2", document))  # compose placements written by this test
    elif str((document.get("meta") or {}).get("app", "")).startswith("agent-sprite-forge export_engine"):
        return []  # Aseprite sprite-sheet JSON (third-party format; export_engine reads it back before publishing)
    elif not _is_qa_envelope(document):
        return None
    if _is_qa_envelope(document) and ("common", "qaEnvelope", document) not in found:
        found.append(("common", "qaEnvelope", document))
    return found


def _file_refs(node, trail: str = "$"):
    if isinstance(node, dict):
        if isinstance(node.get("path"), str) and isinstance(node.get("sha256"), str) and SHA256.match(node["sha256"]):
            yield trail, node["path"], node["sha256"]
        for key, value in node.items():
            yield from _file_refs(value, f"{trail}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _file_refs(value, f"{trail}[{index}]")


def project_problems(root: Path) -> list[str]:
    """Everything wrong with a finished project folder (see the module docstring)."""
    problems: list[str] = []
    needles = set()
    for base in (root, REPO_ROOT, Path.home()):
        for text in (str(base), base.as_posix()):
            needles |= {text.lower(), json.dumps(text)[1:-1].lower()}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if ".stage" in path.name:
            problems.append(f"{rel}: staging leftover")
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            lowered = text.lower()
            hit = next((needle for needle in needles if needle in lowered), None) or (
                DRIVE_PATH.search(text).group(0) if DRIVE_PATH.search(text) else None)
            if hit:
                problems.append(f"{rel}: absolute path ({hit!r})")
        if suffix not in (".json", ".jsonl"):
            continue
        lines = path.read_text(encoding="utf-8").splitlines() if suffix == ".jsonl" else [path.read_text("utf-8")]
        for number, line in enumerate(lines):
            where = rel if suffix == ".json" else f"{rel}:{number + 1}"
            document = json.loads(line)
            contracts = contracts_of(path, document)
            if contracts is None:
                problems.append(f"{where}: not classified (schema {document.get('schema')!r}); "
                                "name its contract in test_e2e_smoke.contracts_of")
                continue
            for domain, name, part in contracts:
                errors = contract_errors(part, domain, name, skill=DOMAIN_SKILL[domain])
                problems += [f"{where}: {domain}/{name}: {error}" for error in errors[:5]]
            for trail, ref, digest in _file_refs(document):
                target = path.parent / ref
                if target.is_dir():
                    continue  # a folder of frames, bound by a digest of its files
                if target.is_file():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        problems.append(f"{where}: {trail} {ref}: sha256 does not match the file")
                elif "/" in ref:  # a bare name is the documented fallback for files outside a package (D30)
                    problems.append(f"{where}: {trail} {ref}: names no file")
    return problems


class Project:
    """A user's project folder: tools run from its root, as a user runs them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.steps: list[str] = []

    def write_json(self, rel: str, document) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=1), encoding="utf-8")
        return rel

    def run(self, skill: str, tool: str, *args, expect: int = 0, timeout: float = 600) -> dict:
        """Run a skill script (Python, or a .mjs under node) and return its one-line JSON summary."""
        argv = [str(arg) for arg in args]
        if tool.endswith(".mjs"):
            completed = subprocess.run([require_node(), str(SKILLS_DIR / skill / "scripts" / tool), *argv],
                                       cwd=self.root, capture_output=True, encoding="cp1252", errors="replace",
                                       timeout=timeout, check=False)
        else:
            completed = run_cli([script_path(skill, tool), *argv], "cp1252", cwd=self.root, timeout=timeout)
        label = f"{tool} {' '.join(argv[:2])}"
        self.steps.append(label)
        if completed.returncode != expect:
            raise AssertionError(f"{label}: exit {completed.returncode}, expected {expect}\n"
                                 f"stdout: {completed.stdout[-2000:]}\nstderr: {completed.stderr[-3000:]}")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not completed.stdout.isascii() or not completed.stderr.isascii():
            raise AssertionError(f"{label}: expected one ASCII JSON line, got {completed.stdout[-2000:]!r} "
                                 f"(stderr {completed.stderr[-500:]!r})")
        return json.loads(lines[0])

    def load(self, rel: str):
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    def assert_clean(self, test: unittest.TestCase) -> None:
        problems = project_problems(self.root)
        test.assertEqual(problems, [], "after " + " -> ".join(self.steps) + ":\n" + "\n".join(problems[:40]))


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.project = Project(Path(self._temporary.name) / "project")
        self.project.root.mkdir()

    # ----------------------------------------------------------------- 1. still

    def test_still_fox_process_scale_clips_engine(self):
        p = self.project
        (p.root / "raw").mkdir()
        shutil.copyfile(real_fixture("raw-fox-run-v1.png"), p.root / "raw" / "fox-run.png")
        processed = p.run("generate2dsprite", "generate2dsprite", "process", "--input", "raw/fox-run.png",
                          "--target", "asset", "--mode", "run", "--rows", "2", "--cols", "4", "--background-mode",
                          "native_alpha", "--resampler", "nearest", "--logical-pixel", "8", "--pixel-scale", "8",
                          "--scale-strategy", "registered", "--align", "feet", "--cell-size", "512",
                          "--output-dir", "work/fox-run")
        self.assertEqual(processed["frames"], 8)
        frames = [f"work/fox-run/run-{index}.png" for index in range(1, 9)]
        scaled = p.run("generate2dsprite", "scale_frames", "--frames", *frames, "--scale-from", "1/8", "--resampler",
                       "nearest", "--root-lock", "torso-x", "--emit-clips", "--clip-name", "run", "--ticks", "5",
                       "--output-dir", "work/fox-game")
        self.assertEqual((scaled["status"], scaled["scale"]), ("pass", 0.125))
        built = p.run("generate2dsprite", "build_animation_clips", "--manifest", "work/fox-game/clips.json",
                      "--output-dir", "work/fox-clips", "--strict")
        self.assertEqual((built["schema"], built["clips"], built["frames"]),
                         ("generate2dsprite.animation_clips.v2", 1, 8))
        exported = p.run("generate2dsprite", "export_engine", "--clips", "work/fox-clips/animation-clips.json",
                         "--target", "all", "--name", "fox", "--output-dir", "game/fox")
        self.assertEqual(exported["targets"], ["aseprite-json", "godot-spriteframes", "godot-sprite3d"])

        # The pixel-art facts travel from scale_frames through the builder into every engine file.
        clips = p.load("work/fox-clips/animation-clips.json")
        run = clips["clips"]["run"]
        self.assertEqual((clips["sampling"], clips["pixel_art"]), ("nearest", True))
        self.assertEqual((run["ticks"], run["tick_hz"], run["total_duration_ms"]), ([5] * 8, 60, 667))
        self.assertEqual(clips["frame_size"], list(p.load("work/fox-game/scale-frames.json")["canvas"]))
        export = p.load("game/fox/engine-export.json")
        self.assertEqual((export["sampling"], export["clips"]["run"]["duration_ms"]),
                         ("nearest", run["duration_ms"]))
        tags = p.load("game/fox/aseprite/fox.json")["meta"]["frameTags"]
        self.assertEqual([(tag["name"], tag["from"], tag["to"]) for tag in tags], [("run", 0, 7)])
        p.assert_clean(self)

    # ----------------------------------------------------------------- 2. video

    def test_video_keyed_clip_to_validated_package(self):
        ffmpeg = require_ffmpeg()
        p = self.project
        (p.root / "art").mkdir()
        runner_frame(0).save(p.root / "art" / "hero.png")
        prepared = p.run("video2dsprite", "prepare_i2v_input", "prepare", "--master", "art/hero.png", "--action",
                         "run", "--canvas", "320,180", "--output-dir", "jobs/hero-run")
        job = p.load("jobs/hero-run/registration_job.json")
        key = {"magenta": (255, 0, 255), "green": (0, 255, 0), "blue": (0, 0, 255)}[prepared["keyColor"]]
        provider_clip(job, [runner_frame(t) for t in range(48)], key, p.root / "takes" / "hero-run-1.mp4", ffmpeg)
        keyed = p.run("video2dsprite", "video2dsprite", "process", "--video", "takes/hero-run-1.mp4", "--output-dir",
                      "work/hero-run", "--matte", "soft", "--reference", "art/hero.png", "--frame-counts", "8")
        self.assertEqual((keyed["frames"], keyed["matte"]["mode"]), (48, "soft"))
        registered = p.run("video2dsprite", "register_clip", "apply", "--job", "jobs/hero-run/registration_job.json",
                           "--frames", "work/hero-run/frames-clean", "--output-dir", "work/hero-run-reg", "--lock",
                           "feet")
        self.assertEqual((registered["frames"], registered["sourceSize"]), (48, job["sourceSize"]))
        loop = p.run("video2dsprite", "gait_loop", "select", "--frames-dir", "work/hero-run-reg/frames", "--fps", "24",
                     "--state", "run", "--output-dir", "work/hero-run-loop")
        self.assertEqual((loop["policy"], loop["frames"]), ("cycle", 16))  # one stride of the 16-frame runner
        timed = p.run("video2dsprite", "retime", "--frames-dir", "work/hero-run-reg/frames", "--fps", "24",
                      "--selection", "work/hero-run-loop/selection.json", "--duration", "640", "--output-dir",
                      "work/hero-run-timed")
        self.assertEqual((timed["frames"], timed["durationMs"]), (16, 640))
        packaged = p.run("video2dsprite", "video2dsprite", "package", "--clean-dir", "work/hero-run-reg/frames",
                         "--selection", "work/hero-run-timed/selection.json", "--registration",
                         "work/hero-run-reg/registration.json", "--output-dir", "game/hero/run", "--name", "run",
                         "--formats", "png,webm,packed", "--loop")
        self.assertEqual((packaged["frameCount"], packaged["durationMs"], packaged["keySource"]),
                         (16, 640, "registration"))
        verified = p.run("video2dsprite", "video2dsprite", "verify", "--package", "game/hero/run")
        self.assertEqual((verified["status"], sorted(verified["transports"])), ("pass", ["packedAlpha", "webm"]))
        validated = p.run("video2dsprite", "validate_animation", "game/hero", "--require-states", "run",
                          "--require-verify", "--report", "game/hero-validation.json")
        self.assertEqual(validated["status"], "pass")

        # The package plays the frames the loop selected, timed by retime, on the registered canvas.
        manifest = p.load("game/hero/run/animation.json")
        selection = p.load("work/hero-run-timed/selection.json")
        self.assertEqual(manifest["durationsMs"], selection["durations_ms"])
        self.assertEqual((manifest["sourceSize"], manifest["sourceAnchor"]), (job["sourceSize"], job["sourceAnchor"]))
        self.assertEqual(p.load("work/hero-run-reg/registration.json")["hygiene"]["floor"], 4)  # D18
        p.assert_clean(self)

    # ----------------------------------------------------------------- 3. code art

    def test_code_art_pixelspec_rig_and_fx(self):
        require_node()
        p = self.project
        art = p.root / "art"
        art.mkdir()
        for name in ("walker16x24.pixelspec.json", "hero.anim.json", "hero.rig.svg", "slash.fx.json"):
            shutil.copyfile(SKILLS_DIR / "codeart2d" / "examples" / name, art / name)
        rendered = p.run("codeart2d", "render_pixelspec", "--spec", "art/walker16x24.pixelspec.json", "--output-dir",
                         "work/walker", "--build-clips", "--strict-qc")
        self.assertEqual(rendered["qa"], "pass")
        bundles = sorted((p.root / "work" / "walker").glob("*/bundle/animation-clips.json"))
        self.assertEqual(len(bundles), len(rendered["variants"]))
        walker = p.run("generate2dsprite", "export_engine", "--clips", bundles[0].relative_to(p.root).as_posix(),
                       "--target", "all", "--output-dir", "game/walker")
        self.assertEqual(walker["status"], "pass")
        rigged = p.run("codeart2d", "rig_animate", "--rig", "art/hero.rig.svg", "--anim", "art/hero.anim.json",
                       "--output-dir", "work/hero", "--route", "pixel", "--build-clips", "--strict-qc")
        self.assertEqual(rigged["qa"], "pass")
        hero = p.run("generate2dsprite", "export_engine", "--clips", "work/hero/compiled-clips/animation-clips.json",
                     "--target", "all", "--output-dir", "game/hero")
        self.assertEqual(hero["status"], "pass")
        effects = p.run("codeart2d", "fx_build", "--spec", "art/slash.fx.json", "--output-dir", "work/fx", "--route",
                        "pixel", "--export-runtime", "--strict-qc")
        verified = p.run("codeart2d", "fx_verify.mjs", "work/fx/fx-runtime.mjs", "--report", "work/fx-verify.json")
        self.assertEqual((verified["status"], verified["failed"]), ("pass", []))
        self.assertEqual(verified["effects"], effects["effects"])

        # Code art reaches the engines as code-made pixel art: nearest sampling, its events kept (D11, D12).
        built = p.load("work/hero/compiled-clips/animation-clips.json")
        self.assertEqual((built["schema"], built["sampling"], built["art_source"]),
                         ("generate2dsprite.animation_clips.v2", "nearest", "code"))
        events = {clip: [event["name"] for event in value["events_ms"]] for clip, value in built["clips"].items()}
        exported = p.load("game/hero/engine-export.json")
        self.assertEqual(exported["sampling"], "nearest")
        self.assertEqual({clip: [event["name"] for event in value["events_ms"]]
                          for clip, value in exported["clips"].items()}, events)
        self.assertTrue(any(events.values()), "the hero's step events survive the builder and the export")
        p.assert_clean(self)

    # ----------------------------------------------------------------- 4. map

    def test_map_tiles_layout_bundle_nav_exports_compose_preview(self):
        p = self.project
        p.write_json("art/shore.material.json", SHORE_MATERIALS)
        p.write_json("art/shore.layout.json", SHORE_LAYOUT)
        tiles = p.run("codeart2d", "autotile_build", "--material-spec", "art/shore.material.json", "--output-dir",
                      "map/tiles", "--preview-map", "none", "--strict-qc")
        self.assertEqual((tiles["status"], len(tiles["tilesets"])), ("pass", 1))
        laid = p.run("codeart2d", "layout_build", "--spec", "art/shore.layout.json", "--tiles", tiles["tilesets"][0],
                     "--output-dir", "map/shore", "--preview", "--strict-qc")
        self.assertEqual((laid["status"], laid["reachable_fraction"]), ("pass", 1.0))
        bundle_rel = "map/shore/map-bundle.json"
        checked = p.run("generate2dmap", "map_bundle", "validate", "--bundle", bundle_rel, "--report",
                        "qa/bundle-report.json", "--require-sha256")
        self.assertEqual((checked["status"], checked["errors"]), ("pass", 0))
        nav = p.run("generate2dmap", "map_nav", "check", "--bundle", bundle_rel, "--output-dir", "qa/nav")
        self.assertEqual((nav["status"], nav["unreachable"]), ("pass", 0))
        tiled = p.run("generate2dmap", "export_tiled", "export", "--bundle", bundle_rel, "--output-dir", "game/tiled",
                      "--embedded-variant")
        self.assertEqual(tiled["differingPixels"], 0)
        reread = p.run("generate2dmap", "export_tiled", "verify", "--map", "game/tiled/map.tmj", "--bundle", bundle_rel)
        self.assertEqual(reread["status"], "pass")
        godot = p.run("generate2dmap", "export_godot", "--bundle", bundle_rel, "--output-dir", "game/godot", "--name",
                      "shore", "--strict-qc")
        self.assertEqual(godot["status"], "pass")
        ldtk = p.run("generate2dmap", "export_ldtk", "--bundle", bundle_rel, "--output-dir", "game/ldtk", "--name",
                     "shore")
        self.assertIn(ldtk["status"], ("pass", "warn"))  # warn: spawns on half pixels are rounded for LDtk

        bundle = p.load(bundle_rel)
        Image.new("RGBA", (8, 12), (220, 60, 40, 255)).save(p.root / "art" / "hero.png")
        actors = [{"id": f"actor-{k}", "image": "art/hero.png", "x": x, "y": y, "anchor": "px", "anchorPx": [4, 11],
                   "layer": "world"} for k, (x, y) in enumerate(ACTOR_SPOTS)]
        props = [{"id": obj["id"], "image": f"map/shore/{bundle['props'][obj['prop']]['image']}", "x": obj["x"],
                  "y": obj["y"], "anchor": "px", "anchorPx": obj["anchor_px"], "layer": "world",
                  "flip_x": obj.get("flip_x", False)} for obj in bundle["objects"]]
        p.write_json("art/placements.json", {"props": props, "actors": actors})
        composed = p.run("generate2dmap", "compose_layered_preview", "--base", "map/shore/preview.png", "--placements",
                         "art/placements.json", "--output", "qa/compose.png", "--report", "qa/compose-report.json",
                         "--bundle", bundle_rel, "--debug-overlay", "qa/compose-debug.png", "--audit-out",
                         "qa/compose-audit.json")
        self.assertEqual(composed["placed"], len(props) + len(actors))
        preview = p.run("generate2dmap", "build_scene_preview", "--bundle", bundle_rel, "--output-dir", "qa/preview")
        self.assertEqual(preview["status"], "pass")

        # One blocking set for every consumer (D2, D4): compose's feet audit answers what map_nav answers.
        audit = {row["id"]: row for row in p.load("qa/compose-audit.json")["placements"]}
        query = p.run("generate2dmap", "map_nav", "query", "--bundle", bundle_rel,
                      *[arg for x, y in ACTOR_SPOTS for arg in ("--point", f"{x},{y}")])
        self.assertEqual([audit[f"actor-{k}"]["foot_valid"] for k in range(len(ACTOR_SPOTS))],
                         [point["valid"] for point in query["points"]])
        self.assertEqual(sorted({point["valid"] for point in query["points"]}), [False, True])
        exported = p.load("game/godot/godot-export.json")
        self.assertEqual(exported["counts"]["objects"], len(bundle["objects"]))
        self.assertGreater(p.load("qa/preview/preview-qa.json")["preview"]["collision"]["tileSolids"], 0)
        p.assert_clean(self)

    # ----------------------------------------------------------------- 5. HD-2D

    def test_hd2d_stage_mask_motion_loop(self):
        require_ffmpeg()
        p = self.project
        plate = hd2d_scene(p.root / "scene")
        staged = p.run("generate2dmap", "validate_stage", "--stage", "scene/stage.json", "--output-dir", "qa/stage")
        self.assertEqual((staged["status"], staged["failed"]), ("needs-visual-review", []))
        masked = p.run("generate2dmap", "build_motion_mask", "--plan", "scene/motion-plan.json", "--plate",
                       "scene/plate.png", "--output-dir", "qa/mask")
        self.assertEqual((masked["status"], masked["protected_max"]), ("pass", 0))
        built = p.run("generate2dmap", "scene_motion", "build", "--plan", "scene/motion-plan.json", "--plate",
                      "scene/plate.png", "--clip", "scene/clip", "--fps", "24", "--mask", "qa/mask/motion-mask.png",
                      "--output-dir", "game/scene-loop")
        self.assertEqual((built["frames"], built["fps"]), (40, "24/1"))  # 48 frames - overlap 8
        self.assertIn(built["qa_status"], ("pass", "warn"))
        loop_qa = p.run("generate2dmap", "scene_motion", "qa", "--report", "game/scene-loop/scene-motion.json",
                        "--plate", "scene/plate.png", "--output-dir", "qa/loop")
        self.assertIn(loop_qa["status"], ("pass", "warn"))

        stage = p.load("scene/stage.json")
        width, height = stage["sourceSize"]
        Image.new("RGBA", (10, 20), (230, 200, 90, 255)).save(p.root / "scene" / "actor.png")
        slots = [*stage["slots"]["hero"], *stage["slots"]["enemy"]]
        actors = [{"id": f"slot-{k}", "image": "scene/actor.png", "x": u * width, "y": v * height, "anchor": "px",
                   "anchorPx": [5, 19], "layer": "world"} for k, (u, v) in enumerate(slots)]
        p.write_json("art/placements.json", {"actors": actors})
        composed = p.run("generate2dmap", "compose_layered_preview", "--base", "game/scene-loop/poster.png",
                         "--placements", "art/placements.json", "--output", "qa/scene.png", "--stage",
                         "scene/stage.json", "--mask", "qa/mask/motion-mask.png", "--debug-overlay",
                         "qa/scene-overlay.png", "--audit-out", "qa/scene-audit.json", "--strict")
        self.assertEqual(composed["audit_status"], "pass")

        # The loop moves only inside the mask, and the mask keeps the stage's motion-protected tower at 0.
        mask = np.asarray(Image.open(p.root / "game" / "scene-loop" / "motion-mask.png"))
        x0, y0, x1, y1 = TOWER
        self.assertEqual(int(mask[y0:y1, x0:x1].max()), 0)
        poster = np.asarray(Image.open(p.root / "game" / "scene-loop" / "poster.png").convert("RGB"))
        np.testing.assert_array_equal(poster[mask == 0], plate[mask == 0])
        self.assertEqual(p.load("game/scene-loop/scene-motion.json")["composite"]["outsideMaskMaxDelta"], 0)
        p.assert_clean(self)


# --------------------------------------------------------------------------- synthetic video take

def _limb(draw, x0, y0, angle, length, width, colour, foot=None, knee=0.0):
    xk, yk = x0 + 0.5 * length * math.sin(angle), y0 + 0.5 * length * math.cos(angle)
    x1, y1 = xk + 0.5 * length * math.sin(angle - knee), yk + 0.5 * length * math.cos(angle - knee)
    draw.line((x0, y0, xk, yk), fill=colour, width=width)
    draw.line((xk, yk, x1, y1), fill=colour, width=width)
    draw.ellipse((xk - width / 2, yk - width / 2, xk + width / 2, yk + width / 2), fill=colour)
    if foot:
        draw.ellipse((x1 - 9, y1 - 6, x1 + 13, y1 + 6), fill=foot)


def runner_frame(t: float, period: int = 16) -> Image.Image:
    """A side-view runner (the B07 test runner, distinct legs) on a 256x256 transparent canvas; frame 0 is the
    master."""
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    phase = 2 * math.pi * t / period
    cx, hip = 128, 150 + 6 * math.cos(2 * phase)
    near, far = 0.6 * math.sin(phase), 0.6 * math.sin(phase + math.pi)
    _limb(draw, cx, hip, far, 70, 18, (28, 36, 60), foot=(110, 70, 40), knee=1.1 * max(0.0, math.cos(phase + math.pi)))
    _limb(draw, cx, hip - 55, -0.7 * math.sin(phase + math.pi), 45, 12, (28, 100, 98))
    draw.rounded_rectangle((cx - 22, hip - 75, cx + 22, hip + 4), 8, fill=(36, 128, 126))
    draw.ellipse((cx - 26, hip - 125, cx + 26, hip - 73), fill=(240, 200, 170))
    _limb(draw, cx, hip, near, 70, 18, (40, 52, 84), foot=(110, 70, 40), knee=1.1 * max(0.0, math.cos(phase)))
    _limb(draw, cx, hip - 55, -0.7 * math.sin(phase), 45, 12, (36, 128, 126))
    return image


def provider_clip(job: dict, frames: list[Image.Image], key: tuple[int, int, int], path: Path, ffmpeg: str) -> None:
    """What an image-to-video provider returns for the job: every pose placed exactly as prepare_i2v_input placed
    the master, over the job's key colour, encoded as H.264 yuv420p at 24 fps."""
    core = load_script("video2dsprite", "forge_core")
    canvas = tuple(job["referenceCanvas"])
    rgb = []
    for frame in frames:
        placed = np.asarray(core.resample_rgba(frame, job["referenceScale"], "lanczos", anchor_src=(0.0, 0.0),
                                               anchor_dst=tuple(job["referenceOffset"]), out_size=canvas))
        alpha = placed[..., 3:].astype(np.float64) / 255
        rgb.append(np.floor(placed[..., :3] * alpha + np.asarray(key, np.float64) * (1 - alpha) + 0.5).astype(np.uint8))
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = rgb[0].shape[:2]
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt",
                    "rgb24", "-s", f"{width}x{height}", "-r", "24", "-i", "pipe:0", "-c:v", "libx264", "-crf", "10",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", str(path)],
                   input=b"".join(np.ascontiguousarray(f).tobytes() for f in rgb), check=True, capture_output=True,
                   timeout=300)


# --------------------------------------------------------------------------- map inputs

SHORE_MATERIALS = {
    "schema": "codeart2d.material_spec.v1", "tile_size": 16, "variants": 1, "seed": 5,
    "materials": {"water": {"ramp": ["#1d3d73", "#2c62ab", "#4b98dc", "#a5dcf5"], "walkable": False,
                            "texture": {"base": 1}},
                  "grass": {"ramp": ["#23502f", "#377a3b", "#5aa344", "#8cc657"], "texture": {"base": 2}}},
    "sets": [{"kind": "wang_corner", "materials": ["water", "grass"]}],
}
SHORE_LAYOUT = {
    "schema": "codeart2d.layout_spec.v1", "map_id": "shore", "size": [24, 14], "tile_size": 16, "seed": 3,
    "materials": {"grass": {"color": "#5aa344"}, "water": {"color": "#2c62ab", "walkable": False}},
    "terrain": [{"id": "pond", "shape": "ellipse", "material": "water", "center": [12, 7], "radius": [4.2, 3.1],
                 "wobble": 0.5}],
    "props": {"tree": {"occlusion": "tall"}, "rock": {}},
    "scatter": [{"id": "trees", "kinds": {"tree": 2, "rock": 1}, "count": 14, "spacing": 22}],
    "exits": [{"id": "west", "edge": "west", "span": [4, 9], "to": "a:from-shore"},
              {"id": "east", "edge": "east", "span": [4, 9], "to": "b:from-shore"}],
    "spawns": [{"id": "start", "x": 30, "y": 112}],
}
ACTOR_SPOTS = [(30, 112), (192, 112), (120, 60), (60, 40), (300, 200), (100, 112), (150, 112)]


# --------------------------------------------------------------------------- HD-2D inputs

PLATE_W, PLATE_H = 256, 144
LAKE = (8, 92, 248, 128)  # source px, half-open box
TOWER = (112, 8, 144, 72)


def _noise(height: int, width: int, cell: int, seed: int) -> np.ndarray:
    small = np.random.default_rng(seed).random((max(2, height // cell), max(2, width // cell))).astype(np.float32)
    return np.asarray(Image.fromarray(small).resize((width, height), Image.Resampling.BICUBIC), np.float32)


def hd2d_scene(folder: Path) -> np.ndarray:
    """plate.png, stage.json (UV), motion-plan.json (px) and clip/ (48 frames whose lake drifts) for one plate;
    returns the plate's RGB pixels."""
    folder.mkdir(parents=True)
    yy = np.mgrid[0:PLATE_H, 0:PLATE_W][0].astype(np.float32)
    plate = np.stack([90 + 60 * yy / PLATE_H, 120 + 50 * yy / PLATE_H, 190 - 60 * yy / PLATE_H], axis=-1)
    plate += ((_noise(PLATE_H, PLATE_W, 2, 1) - 0.5) * 30 + (_noise(PLATE_H, PLATE_W, 9, 2) - 0.5) * 40)[..., None]
    plate[72:92, :] = (150, 140, 120)  # the stone ground band
    plate[TOWER[1]:TOWER[3], TOWER[0]:TOWER[2]] = (110, 110, 120)
    plate = np.clip(plate, 0, 255)
    plate8 = np.rint(plate).astype(np.uint8)
    Image.fromarray(plate8).save(folder / "plate.png")
    u = lambda x: round(x / PLATE_W, 4)  # noqa: E731
    v = lambda y: round(y / PLATE_H, 4)  # noqa: E731
    lake = [[u(LAKE[0]), v(LAKE[1])], [u(LAKE[2]), v(LAKE[1])], [u(LAKE[2]), v(LAKE[3])], [u(LAKE[0]), v(LAKE[3])]]
    stage = {
        "schema": "generate2dmap.stage.v1", "plate": "plate.png", "sourceSize": [PLATE_W, PLATE_H], "fit": "cover",
        "reviewedAspectRange": [1.3333, 2.3333],
        "groundPolygons": [[[0.02, v(70)], [0.98, v(70)], [0.98, v(91)], [0.02, v(91)]]],
        "playableBand": [v(70), v(91)],
        "slots": {"hero": [[0.7, v(84)], [0.84, v(86)]], "enemy": [[0.14, v(84)], [0.28, v(86)]]},
        "protectedRegions": [{"id": "tower", "box": [u(TOWER[0]), v(TOWER[1]), u(TOWER[2]), v(TOWER[3])],
                              "appliesTo": ["walk", "motion", "edit"]}],
        "effects": [{"id": "lake", "kind": "ripple", "polygon": lake, "period": 2, "amplitude": 1, "wavelength": 40,
                     "axis": "x"}],
        "bakedContent": {"actors": False, "collectibles": False},
    }
    (folder / "stage.json").write_text(json.dumps(stage, indent=1), encoding="utf-8")
    plan = {"schema": "generate2dmap.motion_plan.v1", "sourceSize": [PLATE_W, PLATE_H],
            "regions": [{"id": "lake", "kind": "rect", "box": list(LAKE), "feather": 4, "strength": 1,
                         "motion": "flow"}],
            "protected": [{"id": "tower", "box": list(TOWER), "margin": 2}],
            "loop": {"policy": "forward-overlap", "range": [0, 48], "overlap": 8}}
    (folder / "motion-plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    (folder / "clip").mkdir()
    rng = np.random.default_rng(3)
    texture = _noise(PLATE_H, PLATE_W * 4, 2, 11) * 0.6 + _noise(PLATE_H, PLATE_W * 4, 6, 12) * 0.4
    band = np.zeros((PLATE_H, PLATE_W), bool)
    band[LAKE[1]:LAKE[3], LAKE[0]:LAKE[2]] = True
    position = 0.0
    for index in range(48):
        position += 0.6 * (0.15 + 0.85 * max(0.0, math.sin(index / 48 * 2 * math.pi * 1.7 + 0.5)) ** 2)
        whole, fraction = math.floor(position), position - math.floor(position)
        rolled = np.roll(texture, -whole, axis=1)[:, :PLATE_W + 1]
        moving = rolled[:, :PLATE_W] * (1 - fraction) + rolled[:, 1:] * fraction
        frame = plate.copy()
        frame[band] = plate[band] * 0.5 + (np.array([150, 170, 190], np.float32)
                                           + (moving[band][:, None] - 0.5) * 60) * 0.5
        frame += rng.normal(0, 0.6, frame.shape)
        pixels = np.clip(np.rint(frame), 0, 255).astype(np.uint8)
        Image.fromarray(pixels).save(folder / "clip" / f"frame_{index:06d}.png")
    return plate8


if __name__ == "__main__":
    unittest.main()
