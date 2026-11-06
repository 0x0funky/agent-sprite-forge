"""The host API (CONTRACTS section 10 of Agent Game Forge): progress files, the set_state lock primitive, exit 4 for
a published failing result, engine_export.py review, forge_index.py scan and the ledger's published job folder.

Offline and deterministic; ffmpeg is not needed (packages are PNG-only).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import (
    FIXTURES_DIR, REPO_ROOT, SKILLS_DIR, assert_valid_contract, load_shared, run_cli,
)

fc = load_shared("forge_core")
VIDEO = SKILLS_DIR / "video2dsprite" / "scripts"
ENGINE_EXPORT = VIDEO / "engine_export.py"
FINISH_FRAMES = VIDEO / "finish_frames.py"
FORGE_INDEX = SKILLS_DIR / "generate2dmedia" / "scripts" / "forge_index.py"
CONTRACTS = FIXTURES_DIR / "contracts"
sys.path.insert(0, str(SKILLS_DIR / "generate2dmedia" / "scripts"))
import media_ledger  # noqa: E402


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- forge_core.Progress

def test_progress_appends_one_valid_line_per_stage_change_and_never_prints(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(fc.PROGRESS_ENV, raising=False)
    target = tmp_path / "host" / "progress.jsonl"   # the folder is created
    progress = fc.Progress("demo", target)
    progress("load", 0, 3, "frames")
    progress("load", 0, 3, "frames")   # a repeat is not a change
    progress("load", 3, 3, "frames → done")
    progress("done")
    lines = lines_of(target)
    assert [(line["stage"], line["done"], line["total"]) for line in lines] == [("load", 0, 3), ("load", 3, 3),
                                                                                ("done", None, None)]
    assert lines[1]["message"] == "frames -> done" and lines[2]["message"] is None
    for line in lines:
        assert_valid_contract(line, "host", "progress_line_v1")
        assert line["tool"] == "demo"
    assert capsys.readouterr().out == ""
    silent = fc.Progress("demo")   # no flag and no FORGE_PROGRESS_FILE: nothing at all
    assert not silent.enabled
    silent("load")
    monkeypatch.setenv(fc.PROGRESS_ENV, str(tmp_path / "env.jsonl"))
    fc.Progress("demo")("start")
    assert lines_of(tmp_path / "env.jsonl")[0]["stage"] == "start"
    assert fc.PROGRESS_ENV not in fc.child_env_without_progress()


def test_an_unwritable_progress_file_warns_once_and_never_fails_the_run(tmp_path, capsys):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    progress = fc.Progress("demo", blocker / "progress.jsonl")   # its parent is a file
    progress("load")
    progress("done")
    err = capsys.readouterr().err
    assert err.count("warning: the progress file progress.jsonl cannot be written") == 1
    assert not progress.enabled


# --------------------------------------------------------------------------- forge_core.file_lock and exit 4

def test_file_lock_is_exclusive_across_threads_and_processes_and_reentrant(tmp_path):
    lock = tmp_path / "set_state.lock"
    order: list[str] = []

    def contender() -> None:
        with fc.file_lock(lock, timeout=10):
            order.append("thread")

    with fc.file_lock(lock):
        with fc.file_lock(lock):   # the same thread may lock again
            worker = threading.Thread(target=contender)
            worker.start()
            time.sleep(0.3)
            order.append("holder")
    worker.join(timeout=10)
    assert order == ["holder", "thread"]
    script = ("import sys, time; sys.path.insert(0, sys.argv[1]); import forge_core\n"
              "with forge_core.file_lock(sys.argv[2]):\n    print('held', flush=True); time.sleep(2)")
    holder = subprocess.Popen([sys.executable, "-c", script, str(REPO_ROOT / "shared"), str(lock)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="locked by another process"):
            with fc.file_lock(lock, timeout=0.3):
                pass
        assert time.monotonic() - started < 2
        with fc.file_lock(lock, timeout=20):   # waits for the other process, then gets it
            pass
    finally:
        holder.wait(timeout=30)
        holder.stdout.close()


def test_write_json_waits_for_a_reader_that_holds_the_target_open(tmp_path):
    """On Windows a rename onto a file another handle holds open fails (WinError 5); write_json(no_clobber=False)
    waits for the reader instead of failing a run's save. Elsewhere the rename never fails, which this also checks."""
    target = tmp_path / "set_state.json"
    fc.write_json(target, {"n": 0}, no_clobber=False)
    released = threading.Event()

    def reader() -> None:
        with open(target, "rb"):
            released.wait(timeout=10)

    worker = threading.Thread(target=reader)
    worker.start()
    try:
        time.sleep(0.1)
        threading.Timer(0.4, released.set).start()   # the reader lets go within the 2 s replace window
        fc.write_json(target, {"n": 1}, no_clobber=False)
    finally:
        released.set()
        worker.join(timeout=10)
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 1}
    assert [path.name for path in tmp_path.iterdir()] == ["set_state.json"]   # no staged .tmp left behind
    assert fc.json_bytes({"n": 1}) == target.read_bytes()
    if os.name == "nt":   # a reader that never lets go still fails, after the retry window, with the OS error
        with open(target, "rb"):
            started = time.monotonic()
            with pytest.raises(PermissionError):
                fc.replace_file(fc._create_unique(tmp_path, ".x.", ".tmp"), target, retry_s=0.3)
            assert 0.25 <= time.monotonic() - started < 3


def test_exit_4_means_published_with_status_fail():
    assert fc.EXIT_PUBLISHED_FAIL == 4
    assert fc.run_cli(lambda: fc.EXIT_PUBLISHED_FAIL) == 4   # run_cli passes the status through
    for skill in ("generate2dsprite", "generate2dmap", "video2dsprite", "codeart2d"):
        vendored = (SKILLS_DIR / skill / "scripts" / "forge_core.py").read_text(encoding="utf-8")
        assert "EXIT_PUBLISHED_FAIL = 4" in vendored
    for skill in ("generate2dsprite", "generate2dmap", "video2dsprite", "codeart2d"):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(r"exits? 4", text), f"{skill}/SKILL.md documents exit 4"


# --------------------------------------------------------------------------- engine_export.py: progress and review

def make_frames(folder: Path, count: int = 4) -> Path:
    folder.mkdir(parents=True)
    for index in range(count):
        frame = np.zeros((40, 32, 4), np.uint8)
        frame[8 + index:30 + index, 10:22] = (40, 110, 200, 255)
        Image.fromarray(frame, "RGBA").save(folder / f"clean_{index:03d}.png")
    return folder


def verdict(path: Path, reviewed: str, status: str = "accepted", issues=None) -> Path:
    path.write_text(json.dumps({"schema": "video2dsprite.review_verdict.v1", "status": status,
                                "issues": issues or [], "evidence": [], "reviewedSha256": reviewed}), encoding="utf-8")
    return path


@pytest.fixture()
def package(tmp_path):
    frames = make_frames(tmp_path / "clean")
    progress = tmp_path / "package-progress.jsonl"
    result = run_cli([ENGINE_EXPORT, "package", "--clean-dir", frames, "--output-dir", tmp_path / "pkg", "--fps", "12",
                      "--name", "idle", "--progress-file", progress])
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 1
    return tmp_path / "pkg", progress


def test_package_reports_its_stages(package):
    folder, progress = package
    lines = lines_of(progress)
    for line in lines:
        assert_valid_contract(line, "host", "progress_line_v1")
    assert [line["stage"] for line in lines] == ["load", "load", "residue", "publish", "done"]
    assert json.loads((folder / "animation.json").read_text(encoding="utf-8"))["reviewStatus"] == "needs-visual-review"


def test_review_records_a_verdict_in_place_and_keeps_every_binding(package, tmp_path):
    folder, _ = package
    provenance = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    before = {name: (folder / name).read_bytes() for name in os.listdir(folder) if name.endswith(".png")}
    good = verdict(tmp_path / "verdict.json", provenance["inputDigest"])
    result = run_cli([ENGINE_EXPORT, "review", "--package", folder, "--review", good])
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert (summary["reviewStatus"], summary["boundTo"], summary["changed"]) == ("accepted", "inputDigest", True)
    manifest = json.loads((folder / "animation.json").read_text(encoding="utf-8"))
    assert manifest["reviewStatus"] == "accepted" and manifest["qa"]["status"] == "pass"
    assert json.loads((folder / "animation-qa.json").read_text(encoding="utf-8")) == manifest["qa"]
    assert any(item["sha256"] == fc.sha256_file(good) for item in manifest["qa"]["inputs"])
    provenance = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["review"]["status"] == "accepted" and provenance["reviewHistory"][-1]["previousStatus"] == \
        "needs-visual-review"
    assert {name: (folder / name).read_bytes() for name in before} == before   # no re-encode, no new pixels
    assert_valid_contract(manifest, "video", "animation_v3")
    again = run_cli([ENGINE_EXPORT, "review", "--package", folder, "--review", good])
    assert again.returncode == 0 and json.loads(again.stdout)["changed"] is False
    checked = run_cli([VIDEO / "validate_animation.py", folder, "--require-review"])
    assert checked.returncode == 0, checked.stderr + checked.stdout


def test_review_refuses_stale_and_failing_verdicts_and_a_changed_package(package, tmp_path):
    folder, _ = package
    stale = run_cli([ENGINE_EXPORT, "review", "--package", folder, "--review", verdict(tmp_path / "s.json", "0" * 64)])
    assert stale.returncode == 1 and "stale review" in stale.stderr
    digest = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))["inputDigest"]
    failing = verdict(tmp_path / "f.json", digest, "fail_regenerate", [{"desc": "the sword doubles"}])
    refused = run_cli([ENGINE_EXPORT, "review", "--package", folder, "--review", failing])
    assert refused.returncode == 1 and "fail_regenerate" in refused.stderr
    manifest = json.loads((folder / "animation.json").read_text(encoding="utf-8"))
    poster = folder / manifest["poster"]["file"]
    poster.write_bytes(poster.read_bytes() + b"\0")
    changed = run_cli([ENGINE_EXPORT, "review", "--package", folder, "--review", verdict(tmp_path / "g.json", digest)])
    assert changed.returncode == 1 and "changed since it was made" in changed.stderr
    assert json.loads((folder / "animation.json").read_text(encoding="utf-8")) == manifest   # nothing was rewritten


# --------------------------------------------------------------------------- finish_frames.py progress

def test_finish_reports_a_downscale_stage_per_frame(tmp_path):
    frames = tmp_path / "registered"
    frames.mkdir()
    for index in range(3):
        frame = np.zeros((160, 120, 4), np.uint8)
        frame[30:150, 40 + index:80 + index] = (200, 120, 60, 255)
        Image.fromarray(frame, "RGBA").save(frames / f"f{index:03d}.png")
    progress = tmp_path / "finish.jsonl"
    result = run_cli([FINISH_FRAMES, "hd", "--frames", frames, "--output-dir", tmp_path / "out", "--target-height", "40",
                      "--progress-file", progress])
    assert result.returncode == 0, result.stderr
    lines = lines_of(progress)
    for line in lines:
        assert_valid_contract(line, "host", "progress_line_v1")
    downscale = [(line["done"], line["total"]) for line in lines if line["stage"] == "downscale"]
    assert downscale == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert [line["stage"] for line in lines][0] == "load" and lines[-1]["stage"] == "done"
    finish = json.loads((tmp_path / "out" / "finish.json").read_text(encoding="utf-8"))
    assert_valid_contract(finish, "video", "finish_v1")
    failed = run_cli([FINISH_FRAMES, "hd", "--frames", frames, "--output-dir", tmp_path / "out", "--target-height",
                      "40", "--progress-file", progress])
    assert failed.returncode == 1 and lines_of(progress)[-1]["stage"] == "error"


# --------------------------------------------------------------------------- forge_index.py scan

def copy_fixture(name: str, target: Path) -> dict:
    document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document), encoding="utf-8")
    return document


@pytest.fixture()
def art(tmp_path):
    """A project art folder with one of everything, plus the things a scan must skip."""
    root = tmp_path / "art"
    copy_fixture("sprite.master_v1.valid.json", root / "pip" / "master" / "master.json")
    for name in ("master.png", "master_rgba.png"):
        (root / "pip" / "master" / name).write_bytes(b"png")
    set_folder = root / "pip-set"
    copy_fixture("video.sprite_set_plan_v1.valid.json", set_folder / "set_plan.json")
    state = copy_fixture("video.sprite_set_state_v1.valid.json", set_folder / "set_state.json")
    output = state["actions"]["idle"]["output"]
    (set_folder / output["package"]).mkdir(parents=True)
    (set_folder / output["manifest"]).write_text(json.dumps({"reviewStatus": "accepted",
                                                            "poster": {"file": "idle-poster.png"}}), encoding="utf-8")
    (set_folder / output["package"] / "idle-poster.png").write_bytes(b"png")
    copy_fixture("video.sprite_set_plan_v1.valid.json", set_folder / "master" / "side" / "nested" / "set_plan.json")
    copy_fixture("sprite.pixelfx_v1.valid.json", root / "fx" / "star-fall" / "fx.json")
    copy_fixture("sprite.hibit_fx_v1.valid.json", root / "fx" / "burst" / "fx.json")
    copy_fixture("map.hibit_plate_v1.valid.json", root / "plates" / "forest" / "plate.json")
    copy_fixture("map.map_bundle_v2.valid.json", root / "maps" / "forest.map-bundle.json")
    copy_fixture("sprite.master_v1.valid.json", root / ".pip.stage-1a2b3c4d" / "master.json")   # an unpublished stage
    copy_fixture("sprite.master_v1.valid.json", root / "old.orphan-1" / "master.json")
    (root / "broken").mkdir()
    (root / "broken" / "fx.json").write_text("{not json", encoding="utf-8")
    return root


def test_scan_lists_masters_sets_fx_plates_and_maps(art, tmp_path):
    result = run_cli([FORGE_INDEX, "scan", art], "cp1252")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 1 and result.stdout.isascii()
    index = json.loads(result.stdout)
    assert_valid_contract(index, "host", "asset_index_v1")
    assert index["root"] == art.resolve().as_posix()
    assert [item["path"] for item in index["masters"]] == ["pip/master"]   # stage and orphan folders skipped
    assert index["masters"][0]["image"] == "pip/master/master.png"
    pip_set, = index["sets"]   # the set's own folders are never searched (its nested plan is not a second set)
    assert (pip_set["path"], pip_set["state"], pip_set["status"]) == ("pip-set", "pip-set/set_state.json", "complete")
    idle = next(item for item in pip_set["actions"] if item["id"] == "idle")
    assert idle["reviewStatus"] == "accepted" and idle["accepted"]["take"] == 1
    assert idle["manifest"].endswith("package/animation.json") and idle["poster"].endswith("idle-poster.png")
    assert sorted((item["path"], item["route"]) for item in index["fx"]) == [("fx/burst", "B"), ("fx/star-fall", "A")]
    assert [item["path"] for item in index["plates"]] == ["plates/forest"]
    assert [(item["path"], item["schemaVersion"]) for item in index["maps"]] == [
        ("maps/forest.map-bundle.json", "generate2dmap.map_bundle.v2")]
    assert index["warnings"] == ["broken/fx.json: unreadable (JSONDecodeError)"]
    output = tmp_path / "index.json"
    assert run_cli([FORGE_INDEX, "scan", art, "--output", output]).returncode == 0
    assert_valid_contract(json.loads(output.read_text(encoding="utf-8")), "host", "asset_index_v1")
    again = run_cli([FORGE_INDEX, "scan", art, "--output", output])
    assert again.returncode == 1 and "already exists" in again.stderr
    missing = run_cli([FORGE_INDEX, "scan", tmp_path / "nowhere"])
    assert missing.returncode == 1 and missing.stderr.startswith("error: not a folder")


def test_scan_of_an_unrun_set_and_an_empty_folder(tmp_path):
    copy_fixture("video.sprite_set_plan_v1.valid.json", tmp_path / "art" / "slime-set" / "set_plan.json")
    index = json.loads(run_cli([FORGE_INDEX, "scan", tmp_path / "art"]).stdout)
    slime, = index["sets"]
    assert slime["status"] == "planned" and slime["state"] is None
    assert {item["status"] for item in slime["actions"]} == {"planned"}
    assert all(item["package"] is None and item["reviewStatus"] is None for item in slime["actions"])
    empty = tmp_path / "empty"
    empty.mkdir()
    blank = json.loads(run_cli([FORGE_INDEX, "scan", empty]).stdout)
    assert [blank[key] for key in ("masters", "sets", "fx", "plates", "maps", "warnings")] == [[]] * 6


def test_every_vendored_forge_index_is_the_canonical(tmp_path):
    canonical = (REPO_ROOT / "shared" / "forge_index.py").read_bytes().replace(b"\r\n", b"\n")
    entry = next(item for item in json.loads((REPO_ROOT / "shared" / "VENDORED.json").read_text(encoding="utf-8"))[
        "files"] if item["canonical"] == "shared/forge_index.py")
    assert {Path(target).parts[1] for target in entry["targets"]} == {
        "generate2dmedia", "generate2dsprite", "video2dsprite", "generate2dmap"}
    for target in entry["targets"]:
        assert (REPO_ROOT / target).read_bytes().replace(b"\r\n", b"\n") == canonical
    module = load_shared("forge_index")
    assert module.FORGE_PACKAGE_VERSION == fc.FORGE_PACKAGE_VERSION


# --------------------------------------------------------------------------- the ledger names the published folder

def test_the_ledger_records_the_published_job_folder_not_the_stage(tmp_path):
    assert media_ledger.published_dir(".gen.stage-1a2b3c4d/takes/take-01") == "gen/takes/take-01"
    assert media_ledger.published_dir("art\\.fox.stage-k3j_9a2x\\takes\\take-02") == "art/fox/takes/take-02"
    assert media_ledger.published_dir("art/.stage-notes/x") == "art/.stage-notes/x"   # not a staging name
    ledger = media_ledger.Ledger(tmp_path)
    reservation = ledger.reserve({"jobDir": "art/.fox-run.stage-0f1e2d3c/takes/take-01", "fingerprint": "a" * 64,
                                  "provider": "codex", "model": "image_gen", "kind": "image", "route": "codex-cli",
                                  "reservedUsd": 0.0})
    entry = ledger.entries()[reservation]
    assert entry["jobDir"] == "art/fox-run/takes/take-01"
    for line in ledger.lines():
        assert_valid_contract(line, "media", "ledger_line_v1")
