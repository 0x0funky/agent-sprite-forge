"""forge_doctor.py: readiness ladder, ROUTES, drift checks, cold-machine behaviour (plan B22-T1; report v2 P1-2).

Hermetic: PATH points at empty or fixture folders and the home folder at a temporary one, so no
real CLI is found or run; version probes of fake executables are stubbed. Nothing reads a
credential file or contacts anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from forge_testutils import SKILLS_DIR, assert_cli_help, assert_valid_contract, run_cli, script_path

SCRIPTS = SKILLS_DIR / "generate2dmedia" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import cli_media  # noqa: E402  (the instances the scripts themselves import)
import forge_doctor  # noqa: E402
import media_config  # noqa: E402
import media_ledger  # noqa: E402

DOCTOR = script_path("generate2dmedia", "forge_doctor")
FAKES = Path(__file__).parent / "fixtures" / "fake_cli"
EXE = ".exe" if os.name == "nt" else ""
CANARY = "canary-credential-0123456789abcdef"
VERSIONS = {"codex": ("codex-cli 0.155.1", "0.155.1"), "grok": ("grok 1.0.40", "1.0.40")}


def fake_native(path: Path) -> Path:
    """A file that looks like a native executable (PE or ELF header) but is not runnable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"MZ" if os.name == "nt" else b"\x7fELF") + b"\x00" * 64)
    path.chmod(0o755)
    return path


def npm_codex(prefix: Path, *, hoisted: bool = False, native: bool = True) -> tuple[Path, Path | None]:
    """An npm global install of @openai/codex: a script launcher plus (optionally) its native binary."""
    target = forge_doctor._npm_target()
    if target is None:
        pytest.skip("no @openai/codex platform package for this machine")
    triple, package = target
    shim = prefix / ("codex.cmd" if os.name == "nt" else "codex")
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text('@ECHO off\r\nnode "%~dp0node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n' if os.name == "nt"
                    else '#!/bin/sh\nexec node "$(dirname "$0")/node_modules/@openai/codex/bin/codex.js" "$@"\n')
    shim.chmod(0o755)
    root = prefix / "node_modules" / "@openai" / "codex"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "codex.js").write_text("// launcher\n", encoding="utf-8")
    platform_package = root.parent / package if hoisted else root / "node_modules" / "@openai" / package
    if not native:
        return shim, None
    binary = fake_native(platform_package / "vendor" / triple / "bin" / ("codex" + EXE))
    (platform_package / "package.json").write_text(json.dumps(
        {"name": "@openai/codex", "version": "0.155.1-" + package.removeprefix("codex-")}), encoding="utf-8")
    return shim, binary


def cold_env(tmp_path: Path) -> dict:
    """Environment overrides for a machine with no media CLI, no ffmpeg on PATH, no API key and no user
    config file (APPDATA and XDG_CONFIG_HOME point inside the temporary home)."""
    (tmp_path / "empty-bin").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return {"PATH": str(tmp_path / "empty-bin"), "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home"),
            "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"), "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
            "CODEX_HOME": "", "GROK_HOME": "", "OPENAI_API_KEY": "", "XAI_API_KEY": "", "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "", "ARK_API_KEY": "", "FAL_KEY": "", "FORGE_CODEX_EXE": "",
            "FORGE_GROK_EXE": "", "FORGE_SESSION_IMAGES": "", "FORGE_SESSION_VIDEOS": "", "FORGE_SESSION_HOURS": ""}


@pytest.fixture
def cold(tmp_path, monkeypatch):
    """The same cold machine for in-process calls; returns the project folder (also the cwd)."""
    for name, value in cold_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def doctor_json(tmp_path: Path, *args: str, encoding: str | None = None, env: dict | None = None):
    result = run_cli([DOCTOR, "--json", *args], encoding, cwd=tmp_path, env={**cold_env(tmp_path), **(env or {})},
                     timeout=120)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return result, json.loads(lines[-1]) if lines else None


def check(report: dict, check_id: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == check_id)


def stub_versions(monkeypatch):
    monkeypatch.setattr(forge_doctor, "probe_version", lambda argv, cli, timeout=0: VERSIONS[cli])


def write_proofs(project: Path, records: list[dict]) -> None:
    (project / ".forge").mkdir(exist_ok=True)
    (project / ".forge" / "route-proofs.json").write_text(json.dumps(
        {"schema": forge_doctor.PROOFS_SCHEMA, "proofs": records}), encoding="utf-8")


def proof(route: str, tool: str, version: str, level: str = "VERIFIED", recipe: str | None = None) -> dict:
    recipe = recipe or next(c.recipe for c in forge_doctor.CAPABILITIES if (c.route, c.tool) == (route, tool))
    return {"route": route, "tool": tool, "version": version, "recipe": recipe, "level": level,
            "verifiedAt": "2026-10-05T06:00:00.000Z", "runId": "ab" * 16, "artifactSha256": "cd" * 32}


# --------------------------------------------------------------------------- acceptance (plan B22-T1)

def test_help_works_under_cp1252_and_cp950():
    assert_cli_help("generate2dmedia", "forge_doctor")


def test_cold_machine_reports_video_none(tmp_path):
    result, report = doctor_json(tmp_path)
    assert result.returncode == 0, result.stderr
    assert_valid_contract(report, "media", "doctor_v1", skill="generate2dmedia")
    assert report["routes"]["video"]["route"] == "none" and report["routes"]["image"]["route"] == "none"
    assert report["routes"]["clip"]["route"] == "png-frames" and report["routes"]["video"]["options"] == []
    assert report["apiKeys"] == {"openai": False, "gemini": False, "xai": False, "byteplus": False, "fal": False}
    assert report["routeOrder"] == {"image": ["codeart2d"], "image_edit": ["codeart2d"], "video": ["codeart2d"]}
    assert report["routes"]["code_art"]["status"] == "fallback" and "explicit-only" in report["routes"]["code_art"]["detail"]
    assert check(report, "media.config")["status"] == "OK"
    assert check(report, "media.host-tools")["status"] == "AGENT"
    assert check(report, "ffmpeg")["status"] == "MISSING" and check(report, "cli.grok")["status"] == "MISSING"
    assert check(report, "media.api.xai")["status"] == "MISSING" and report["cli"] == {}
    assert check(report, "skills.canonical")["status"] == "OK"  # this checkout's vendored copies are in sync
    assert result.stdout.isascii()


def test_host_tools_add_routes(tmp_path):
    _, report = doctor_json(tmp_path, "--host-tools", "image_gen")
    image = report["routes"]["image"]
    assert (image["route"], image["status"]) == ("host_image", "ready") and report["routes"]["video"]["route"] == "none"
    _, report = doctor_json(tmp_path, "--host-tools", "ImageGen, image-to-video,warp_drive")
    assert report["host"] == {"declared": True, "tools": ["image_gen", "image_to_video"], "unrecognised": ["warp_drive"]}
    assert report["routes"]["video"]["route"] == "host_video"
    assert check(report, "media.host-tools.names")["status"] == "WARN"
    _, report = doctor_json(tmp_path, "--host-tools", "none")
    assert check(report, "media.host-tools")["status"] == "OK" and report["routes"]["image"]["route"] == "none"


def test_cp1252_console_is_fail(tmp_path):
    result, report = doctor_json(tmp_path, encoding="cp1252")
    assert result.returncode == 1 and result.stdout.isascii()
    stdout = check(report, "encoding.stdout")
    assert stdout["status"] == "FAIL" and "U+2192" in stdout["detail"] and "PYTHONUTF8=1" in stdout["remedy"]
    assert "error: 1 check(s) failed: encoding.stdout" in result.stderr


AUDIT = r"""
import json, runpy, sys
opened, spawned = [], []
def hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        opened.append(args[0] if isinstance(args[0], str) else args[0].decode("utf-8", "replace"))
    elif event == "subprocess.Popen":  # Windows passes the command line as one string
        spawned.append(args[1] if isinstance(args[1], str) else " ".join(str(part) for part in args[1]))
sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = sys.argv[1:]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    pass
print("AUDIT " + json.dumps({"opened": opened, "spawned": spawned}), file=sys.stderr)
"""


def test_no_credential_reads(tmp_path):
    """The doctor finds both CLIs and probes them, yet opens none of the sign-in files beside them."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    secrets = [home / ".codex" / "auth.json", home / ".codex" / "config.toml", home / ".grok" / "auth.json",
               home / ".grok" / "config.toml", home / ".grok" / "auth.json.lock", home / ".config" / "xai" / "credentials",
               project / ".env"]
    for path in secrets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"token": "{CANARY}"}}\n', encoding="utf-8")
    fake_native(home / ".grok" / "bin" / ("grok" + EXE))
    shim, binary = npm_codex(tmp_path / "npm")
    result = run_cli(["-c", AUDIT, DOCTOR, "--json", "--probe-auth", "--project-dir", str(project)], cwd=project,
                     env={**cold_env(tmp_path), "PATH": str(shim.parent), "OPENAI_API_KEY": CANARY}, timeout=120)
    audit = json.loads(result.stderr.rsplit("AUDIT ", 1)[1])
    report = json.loads(result.stdout.splitlines()[-1])
    assert check(report, "cli.codex")["status"] == "OK" and check(report, "cli.grok")["status"] == "OK"
    opened = {os.path.normcase(os.path.abspath(p)) for p in audit["opened"]}
    assert not opened & {os.path.normcase(str(p)) for p in secrets}
    assert not [p for p in opened if os.path.normcase(str(home / ".codex")) in p]
    assert CANARY not in result.stdout and CANARY not in result.stderr
    assert audit["spawned"]  # the fake executables were probed (and failed to start)
    for command in audit["spawned"]:  # only local version and sign-in status probes, never a generation
        assert command.endswith(("--version", "login status")), command


@pytest.mark.perf
def test_doctor_runs_under_one_second(tmp_path, cold, monkeypatch):
    _, report = doctor_json(tmp_path)
    assert report["elapsedMs"] < 1000
    # With both CLIs and ffmpeg present, the slow probes run concurrently, not one after another.
    fake_native(tmp_path / "grok-home" / "bin" / ("grok" + EXE))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))

    def slow_version(argv, cli, timeout=0):
        time.sleep(0.3)
        return VERSIONS[cli]

    def slow_ffmpeg():
        time.sleep(0.3)
        return {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "version": "8.0.1", "libvpx": True, "libx264": True}

    monkeypatch.setattr(forge_doctor, "probe_version", slow_version)
    monkeypatch.setattr(forge_doctor, "ffmpeg_probe", slow_ffmpeg)
    started = time.perf_counter()
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert time.perf_counter() - started < 1.0 and report["elapsedMs"] < 1000
    assert report["cli"]["codex-cli:image_gen"]["version"] == "0.155.1"


# --------------------------------------------------------------------------- executables, ladder and routes

@pytest.mark.parametrize("hoisted", [False, True])
def test_npm_launcher_resolves_to_the_native_binary(tmp_path, cold, monkeypatch, hoisted):
    shim, binary = npm_codex(tmp_path / "npm", hoisted=hoisted)
    monkeypatch.setenv("PATH", str(shim.parent))
    info = forge_doctor.resolve_cli("codex")
    assert (info.path, info.shim, info.source) == (binary, shim, "npm")
    assert forge_doctor.package_version(info) == "0.155.1"
    report = forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert "codex 0.155.1 (package.json)" in check(report, "cli.codex")["detail"]


def test_a_launcher_without_its_binary_is_never_run(tmp_path, cold, monkeypatch):
    shim, _ = npm_codex(tmp_path / "npm", native=False)
    monkeypatch.setenv("PATH", str(shim.parent))
    monkeypatch.setattr(forge_doctor, "run_quiet", lambda *a, **k: pytest.fail("nothing may run a launcher"))
    info = forge_doctor.resolve_cli("codex")
    assert info.path is None and info.shim == shim and "launcher" in info.problem
    report = forge_doctor.diagnose(project_dir=cold, console_encoding="utf-8")
    assert check(report, "cli.codex")["status"] == "WARN" and report["cli"]["codex-cli:image_gen"]["level"] is None
    assert report["routes"]["image"]["options"] == []
    monkeypatch.setenv("FORGE_CODEX_EXE", str(shim))  # an override must be native too
    assert forge_doctor.resolve_cli("codex").problem.startswith("FORGE_CODEX_EXE")


def test_npm_target_reads_the_windows_architecture_without_a_child_process(monkeypatch):
    """r3-platform finding 1: on Windows with Python 3.10 or 3.11, platform.machine() runs win32_ver(), which
    spawns `cmd /c ver` (test_no_credential_reads then fails). On win32 the doctor reads PROCESSOR_ARCHITEW6432
    or PROCESSOR_ARCHITECTURE, as platform itself does; other systems still ask platform.machine()."""
    monkeypatch.setattr(forge_doctor.platform, "machine",
                        lambda: pytest.fail("platform.machine() spawns `cmd /c ver` on Windows with Python <= 3.11"))
    monkeypatch.setattr(forge_doctor.sys, "platform", "win32")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    assert forge_doctor._npm_target() == ("x86_64-pc-windows-msvc", "codex-win32-x64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")
    assert forge_doctor._npm_target() == ("aarch64-pc-windows-msvc", "codex-win32-arm64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "x86")  # 32-bit Python on 64-bit Windows (WOW64)
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "AMD64")
    assert forge_doctor._npm_target() == ("x86_64-pc-windows-msvc", "codex-win32-x64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432")
    assert forge_doctor._npm_target() is None  # codex ships no 32-bit Windows package
    monkeypatch.setattr(forge_doctor.sys, "platform", "linux")
    monkeypatch.setattr(forge_doctor.platform, "machine", lambda: "aarch64")
    assert forge_doctor._npm_target() == ("aarch64-unknown-linux-musl", "codex-linux-arm64")


def test_ladder_levels_follow_version_keyed_proofs(tmp_path, cold, monkeypatch):
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    fake_native(tmp_path / "grok-home" / "bin" / ("grok" + EXE))
    stub_versions(monkeypatch)
    write_proofs(cold, [proof("codex-cli", "image_gen", "0.155.1"),
                        proof("grok-cli", "image_gen", "1.0.30"),
                        proof("grok-acp", "image_to_video", "1.0.40", level="TOOL_EXPOSED"),
                        proof("grok-cli", "image_edit", "1.0.40", recipe="grok-headless-image/0")])
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert_valid_contract(report, "media", "doctor_v1", skill="generate2dmedia")
    levels = {key: ladder["level"] for key, ladder in report["cli"].items()}
    assert levels == {"codex-cli:image_gen": "VERIFIED", "grok-cli:image_gen": "PRESENT",
                      "grok-cli:image_edit": "PRESENT", "grok-acp:image_to_video": "TOOL_EXPOSED"}
    steps = {s["step"]: s["status"] for s in report["cli"]["grok-cli:image_gen"]["steps"]}
    assert steps == {"PRESENT": "OK", "AUTH_MODE": "UNKNOWN", "TOOL_EXPOSED": "UNKNOWN", "VERIFIED": "WARN"}
    # Owner decision 2026-10-06: an installed CLI is ready; its first successful run records the proof.
    image = report["routes"]["image"]
    assert (image["route"], image["status"]) == ("local:codex-cli", "ready")
    assert image["options"][0]["label"] == "Codex (local CLI)" and image["options"][0]["verified"] is True
    assert "verified for 0.155.1" in image["detail"]
    assert image["options"][1]["route"] == "local:grok-cli" and image["options"][1]["verified"] is False
    video = report["routes"]["video"]
    assert (video["route"], video["status"]) == ("local:grok-acp", "ready")
    assert "first successful run records the VERIFIED proof" in video["detail"]
    assert check(report, "route.codex-cli:image_gen")["status"] == "OK"
    assert "--verify-route grok-acp" in check(report, "route.grok-acp:image_to_video")["remedy"]
    assert report["proofs"]["grok-cli:image_gen"]["matchesInstalled"] is False
    monkeypatch.setenv("XAI_API_KEY", "set-but-never-read")
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert (report["routes"]["video"]["route"], report["routes"]["video"]["status"]) == ("api:xai", "ready")
    assert report["routeOrder"]["video"] == ["api:xai", "local:grok-acp", "codeart2d"]
    assert "set-but-never-read" not in json.dumps(report)


def test_auth_probe_can_block_a_verified_route(tmp_path, cold, monkeypatch):
    real_probe = forge_doctor.probe_codex_login
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))
    stub_versions(monkeypatch)
    write_proofs(cold, [proof("codex-cli", "image_gen", "0.155.1")])
    for mode, status, route in (("chatgpt", "OK", "local:codex-cli"), ("none", "MISSING", "none"),
                                ("api-key", "WARN", "none")):
        monkeypatch.setattr(forge_doctor, "probe_codex_login", lambda argv, timeout=0, mode=mode: mode)
        report = forge_doctor.diagnose(project_dir=cold, probe_auth=True, console_encoding="utf-8")
        assert check(report, "cli.codex.auth")["status"] == status and report["routes"]["image"]["route"] == route
    for text, mode in (("Logged in using ChatGPT", "chatgpt"), ("Not logged in", "none"),
                       ("Logged in using an API key - sk-***", "api-key"), ("???", "unknown")):
        result = subprocess.CompletedProcess([], 0, "", text)
        monkeypatch.setattr(forge_doctor, "run_quiet", lambda *a, result=result, **k: result)
        assert real_probe(["codex"]) == mode


def test_fake_cli_versions_are_read_with_the_isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("GITHUB_TOKEN", CANARY)
    assert forge_doctor.probe_version([sys.executable, str(FAKES / "fake_grok.py")], "grok") == ("grok 1.0.40", "1.0.40")
    entry = json.loads((tmp_path / "fake.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["credentialEnv"] == [] and entry["isolation"]["GROK_DISABLE_AUTOUPDATER"] == "true"


# --------------------------------------------------------------------------- drift, ffmpeg, encoding, ledger

def test_skill_drift_is_reported(tmp_path, cold):
    root = tmp_path / "skills"
    for skill in forge_doctor.SKILLS:
        (root / skill).mkdir(parents=True)
        (root / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
    for skill in ("generate2dsprite", "generate2dmap"):
        (root / skill / "scripts").mkdir()
        (root / skill / "scripts" / "forge_core.py").write_bytes(b"VALUE = 1\r\n" if skill == "generate2dmap" else b"VALUE = 1\n")
    report = forge_doctor.diagnose(project_dir=cold, skills_root=root, run_versions=False, console_encoding="utf-8")
    assert check(report, "skills.present")["status"] == "OK" and check(report, "skills.vendored")["status"] == "OK"
    (root / "generate2dmap" / "scripts" / "forge_core.py").write_text("VALUE = 2\n", encoding="utf-8")
    files = [p for p in root.rglob("*") if p.is_file()]
    manifest = {"schema": "agent-sprite-forge.install.v1", "skills": list(forge_doctor.SKILLS),
                "files": [{"path": p.relative_to(root).as_posix(), "sha256": forge_doctor.file_sha256(p)} for p in files]}
    (root / forge_doctor.INSTALL_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    report = forge_doctor.diagnose(project_dir=cold, skills_root=root, run_versions=False, console_encoding="utf-8")
    assert "scripts/forge_core.py" in check(report, "skills.vendored")["detail"]
    assert check(report, "skills.install")["status"] == "OK"
    # D24: what running an installed skill writes (bytecode) and OS junk is never drift.
    (root / "codeart2d" / "scripts" / "__pycache__").mkdir(parents=True)
    (root / "codeart2d" / "scripts" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\0")
    (root / "generate2dmap" / ".DS_Store").write_bytes(b"junk")
    (root / "generate2dmap" / "Thumbs.db").write_bytes(b"junk")
    report = forge_doctor.diagnose(project_dir=cold, skills_root=root, run_versions=False, console_encoding="utf-8")
    assert check(report, "skills.install")["status"] == "OK"
    (root / "generate2dsprite" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    (root / "codeart2d" / "scripts" / "stray_tool.py").write_text("print('not shipped')\n", encoding="utf-8")
    (root / "video2dsprite" / "SKILL.md").unlink()
    report = forge_doctor.diagnose(project_dir=cold, skills_root=root, run_versions=False, console_encoding="utf-8")
    install = check(report, "skills.install")
    assert install["status"] == "WARN" and "1 changed, 1 missing, 1 extra" in install["detail"]
    assert "pyc" not in install["detail"] and "DS_Store" not in install["detail"]
    assert "video2dsprite" in check(report, "skills.present")["detail"]


@pytest.mark.parametrize("version, config, statuses", [
    ("8.0.1-full_build-www.gyan.dev", "--enable-libvpx --enable-libx264", {"ffmpeg": "OK"}),
    ("n7.0", "--enable-libvpx", {"ffmpeg": "OK", "ffmpeg.libx264": "WARN"}),
    ("4.4.2-0ubuntu0.22.04.1", "--enable-libvpx --enable-libx264", {"ffmpeg": "FAIL"}),
    ("N-112345-gabcdef", "--enable-libvpx --enable-libx264", {"ffmpeg": "WARN"}),
])
def test_ffmpeg_version_and_encoders(version, config, statuses):
    info = {"ffmpeg": "/x/ffmpeg", "ffprobe": "/x/ffprobe", "version": version,
            "libvpx": "--enable-libvpx" in config, "libx264": "--enable-libx264" in config}
    found = {c.id: c.status for c in forge_doctor.ffmpeg_checks(info)}
    assert found == statuses
    assert all(c.remedy for c in forge_doctor.ffmpeg_checks(info) if c.status in ("FAIL", "MISSING"))


def test_encoding_statuses():
    status = {name: forge_doctor.encoding_checks(name)[0].status for name in ("utf-8", "UTF8", "cp65001", "cp950",
                                                                             "cp1252", "ascii")}
    assert status == {"utf-8": "OK", "UTF8": "OK", "cp65001": "OK", "cp950": "WARN", "cp1252": "FAIL", "ascii": "FAIL"}


def test_unsettled_ledger_reservations_are_reported(cold):
    ledger = media_ledger.Ledger(cold)
    reservation = ledger.reserve({"jobDir": "out/a", "fingerprint": "ab" * 32, "provider": "xai", "model": "m",
                                  "kind": "video", "route": "rest", "reservedUsd": 0.5})
    ledger.commit(reservation, status="unknown")
    report = forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert check(report, "media.ledger")["status"] == "WARN" and "settle" in check(report, "media.ledger")["remedy"]


# --------------------------------------------------------------------------- the three standard CLI tests and --save

def test_save_writes_the_report_and_refuses_an_existing_file(tmp_path):
    target = tmp_path / "outputs" / "doctor.json"
    result, report = doctor_json(tmp_path, "--save", str(target))
    assert result.returncode == 0 and report["saved"] == str(target)
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert_valid_contract(saved, "media", "doctor_v1", skill="generate2dmedia")
    before = target.read_bytes()
    result, _ = doctor_json(tmp_path, "--save", str(target))
    assert result.returncode == 1 and "error: refusing to replace" in result.stderr and target.read_bytes() == before


def test_a_report_that_breaks_its_contract_publishes_nothing(tmp_path, cold, monkeypatch, capsys):
    monkeypatch.setattr(forge_doctor, "path_checks", lambda cwd, root: [forge_doctor.Check("paths.broken", "FAIL", "x")])
    target = tmp_path / "out" / "doctor.json"
    assert forge_doctor.main(["--save", str(target), "--no-exec", "--project-dir", str(cold)]) == 1
    assert "doctor_v1" in capsys.readouterr().err
    assert not target.exists() and not (tmp_path / "out").exists()  # validated before anything is written


def test_verify_route_is_a_dry_run_until_execute(tmp_path, cold, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a dry run must not start a process"))
    assert forge_doctor.main(["--verify-route", "codex-cli", "--project-dir", str(cold)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution"] == "dry-run" and plan["consent"] == "quota" and plan["command"][-1] == "--execute"
    assert not (cold / ".forge").exists()
    assert forge_doctor.main(["--execute"]) == 1 and "only applies to --verify-route" in capsys.readouterr().err


@pytest.mark.parametrize("route, need", [("codex-cli", "image"), ("grok-acp", "video")])
def test_verify_route_execute_records_a_proof_the_ladder_uses(tmp_path, cold, monkeypatch, capsys, route, need):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "fake.log"))
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)

    def fake(cli):
        path = FAKES / f"fake_{cli}.py"
        return cli_media.RouteCli([sys.executable, str(path)], forge_doctor.CliInfo(cli, path=path, source="test"))

    monkeypatch.setattr(cli_media, "resolve_route_cli", fake)
    assert forge_doctor.main(["--verify-route", route, "--execute", "--project-dir", str(cold)]) == 0, capsys.readouterr()
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["status"] == "done" and ".forge" in summary["output"]
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))
    fake_native(tmp_path / "grok-home" / "bin" / ("grok" + EXE))
    stub_versions(monkeypatch)
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert (report["routes"][need]["route"], report["routes"][need]["status"]) == (f"local:{route}", "ready")
    assert report["routes"][need]["options"][0]["verified"] is True


# --------------------------------------------------------------------------- D22, D23, D27, D29


def fake_route_clis(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setenv("FAKE_CLI_LOG", str(tmp_path / "fake.log"))
    for name in ("FAKE_CLI_MODE", "FAKE_CODEX_VERSION", "FAKE_GROK_VERSION", "FAKE_THREAD_ID", "FAKE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)

    def fake(cli):
        path = FAKES / f"fake_{cli}.py"
        return cli_media.RouteCli([sys.executable, str(path)], forge_doctor.CliInfo(cli, path=path, source="test"))

    monkeypatch.setattr(cli_media, "resolve_route_cli", fake)


def test_reverification_after_a_cli_update(tmp_path, cold, monkeypatch, capsys):
    """D23 (reviewer b22_spot.py case 1): after a CLI update the doctor's own remedy, --verify-route
    --execute, must work again. The installed version is part of the verification request's fingerprint and
    a consented verification may repeat an identical earlier one."""
    fake_route_clis(monkeypatch, tmp_path)
    assert forge_doctor.main(["--verify-route", "codex-cli", "--execute", "--project-dir", str(cold)]) == 0, \
        capsys.readouterr().err
    capsys.readouterr()
    monkeypatch.setenv("FAKE_CODEX_VERSION", "codex-cli 0.156.0")
    code = forge_doctor.main(["--verify-route", "codex-cli", "--execute", "--project-dir", str(cold)])
    err = capsys.readouterr().err
    assert code == 0 and "DUPLICATE" not in err, err
    # Verifying the same version again (an explicit, consented re-check) is not refused either.
    assert forge_doctor.main(["--verify-route", "codex-cli", "--execute", "--project-dir", str(cold)]) == 0
    capsys.readouterr()
    proofs = json.loads((cold / ".forge" / "route-proofs.json").read_text(encoding="utf-8"))
    assert_valid_contract(proofs, "media", "route_proofs_v1", skill="generate2dmedia")
    assert sorted((p["version"], p["level"]) for p in proofs["proofs"]) == [("0.155.1", "VERIFIED"),
                                                                           ("0.156.0", "VERIFIED")]
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((cold / ".forge" / "cli-runs").glob("*.json"))]
    assert sorted(r["options"]["verifies"] for r in records) == ["0.155.1", "0.156.0", "0.156.0"]
    by_version = {r["options"]["verifies"]: r["fingerprint"] for r in records}
    assert by_version["0.155.1"] != by_version["0.156.0"]
    assert [s["status"] for s in media_ledger.Ledger(cold).entries().values()] == ["done"] * 3
    # The ladder now reads VERIFIED for the updated CLI.
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))
    monkeypatch.setattr(forge_doctor, "probe_version", lambda argv, cli, timeout=0: ("codex-cli 0.156.0", "0.156.0"))
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert report["cli"]["codex-cli:image_gen"]["level"] == "VERIFIED"


def test_routes_follow_the_owner_order(tmp_path, cold, monkeypatch):
    """Owner decision 2026-10-06: the API when a key is configured (OpenAI, then xAI for images; xAI for video),
    then local (the host tool, then Codex (local CLI), then Grok (local CLI)), then codeart2d. The image_edit
    remedy names the run that really verifies edits."""
    monkeypatch.setenv("FORGE_CODEX_EXE", str(fake_native(tmp_path / "bin" / ("codex" + EXE))))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    fake_native(tmp_path / "grok-home" / "bin" / ("grok" + EXE))
    monkeypatch.setenv("OPENAI_API_KEY", "set-but-never-read")
    monkeypatch.setenv("XAI_API_KEY", "set-but-never-read")
    stub_versions(monkeypatch)
    write_proofs(cold, [proof("codex-cli", "image_gen", "0.155.1"), proof("grok-cli", "image_gen", "1.0.40"),
                        proof("grok-acp", "image_to_video", "1.0.40")])
    report = forge_doctor.diagnose(host_tools="image_gen", project_dir=cold, console_encoding="utf-8")
    assert report["apiKeys"] == {"openai": True, "gemini": False, "xai": True, "byteplus": False, "fal": False}
    image = report["routes"]["image"]
    assert [(o["route"], o["status"]) for o in image["options"]] == [
        ("api:openai", "ready"), ("api:xai", "ready"), ("host_image", "ready"), ("local:codex-cli", "ready"),
        ("local:grok-cli", "ready")]
    assert image["route"] == "api:openai" and image["options"][0]["model"] == "gpt-image-2.5-sunburst"
    assert [o["label"] for o in image["options"][3:5]] == ["Codex (local CLI)", "Grok (local CLI, one-shot image mode)"]
    assert report["routeOrder"]["image"] == ["api:openai", "api:xai", "host_image", "local:codex-cli",
                                             "local:grok-cli", "codeart2d"]
    video = report["routes"]["video"]
    assert [(o["route"], o["status"]) for o in video["options"]] == [("api:xai", "ready"), ("local:grok-acp", "ready")]
    assert video["options"][0]["model"] == "grok-imagine-video-1.5"
    assert video["options"][1]["label"] == "Grok (local CLI, ACP video mode)"
    text = forge_doctor.render_text(report)
    assert "api keys    openai=yes  gemini=no  xai=yes  byteplus=no  fal=no" in text
    assert "image: api:openai > api:xai > host_image" in text
    assert "set-but-never-read" not in text and "set-but-never-read" not in json.dumps(report)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("XAI_API_KEY", "")
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, console_encoding="utf-8")
    assert report["routes"]["image"]["route"] == "local:codex-cli"
    edit = report["routes"]["image_edit"]
    assert edit["route"] == "local:codex-cli" and edit["options"][-1]["route"] == "local:grok-cli"
    assert edit["options"][-1]["verified"] is False
    remedy = check(report, "route.grok-cli:image_edit")["remedy"]
    assert "cli_media.py\" edit --route grok-cli" in remedy and "verifies image_gen only" in remedy


def user_config(**fields) -> Path:
    """The user config file where media_config looks for it (APPDATA / XDG_CONFIG_HOME of cold_env)."""
    path = media_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_keys_in_the_user_config_count_as_configured(tmp_path, cold, monkeypatch):
    """The doctor reads the user config file in-process and reports yes or no only, never a key."""
    user_config(XAI_API_KEY=CANARY, models={"xai-video": "grok-imagine-video-1.5-lite"})
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert report["apiKeys"] == {"openai": False, "gemini": False, "xai": True, "byteplus": False, "fal": False}
    assert check(report, "media.api.xai")["status"] == "UNKNOWN" and "user config file" in check(report, "media.api.xai")["detail"]
    assert check(report, "media.api.openai")["status"] == "MISSING"
    video = report["routes"]["video"]
    assert (video["route"], video["options"][0]["model"]) == ("api:xai", "grok-imagine-video-1.5-lite")
    assert report["routeOrder"]["image"] == ["api:xai", "codeart2d"]
    assert_valid_contract(report, "media", "doctor_v1", skill="generate2dmedia")
    result, shown = doctor_json(tmp_path, env={"APPDATA": os.environ["APPDATA"],
                                               "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]})
    assert shown["apiKeys"] == {"openai": False, "gemini": False, "xai": True, "byteplus": False, "fal": False}
    text = run_cli([DOCTOR], cwd=tmp_path, env={**cold_env(tmp_path), "APPDATA": os.environ["APPDATA"],
                                                 "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]}, timeout=120)
    assert "openai=no  gemini=no  xai=yes" in text.stdout
    for output in (json.dumps(report), result.stdout, result.stderr, text.stdout, text.stderr):
        assert CANARY not in output
    # A damaged file is a warning and its keys are ignored; its content is never quoted.
    user_config().write_text("{broken " + CANARY, encoding="utf-8")
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert check(report, "media.config")["status"] == "WARN" and not any(report["apiKeys"].values())
    assert CANARY not in json.dumps(report)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_world_readable_user_config_is_a_warning(cold):
    path = user_config(OPENAI_API_KEY=CANARY)
    path.chmod(0o644)
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert check(report, "media.config")["status"] == "WARN" and "chmod 600" in check(report, "media.config")["remedy"]
    path.chmod(0o600)
    report = forge_doctor.diagnose(host_tools="none", project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert check(report, "media.config")["status"] == "OK"


def test_one_package_version_and_clean_internal_errors(cold, monkeypatch, capsys):
    """D29: the doctor, the ledger and forge_core name one package version. D27: an unexpected error is one
    line, exit 1, never a traceback."""
    from forge_testutils import load_shared

    assert forge_doctor.FORGE_PACKAGE_VERSION == media_ledger.FORGE_PACKAGE_VERSION \
        == load_shared("forge_core").FORGE_PACKAGE_VERSION == "0.4.0"
    report = forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8")
    assert report["tool"] == {"name": "forge_doctor", "version": "0.4.0"}

    def broken(**kwargs):
        raise RuntimeError("unexpected state")
    monkeypatch.setattr(forge_doctor, "diagnose", broken)
    assert forge_doctor.main(["--project-dir", str(cold)]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: internal error (RuntimeError: unexpected state)" and not captured.out


def test_ledger_check_reports_the_session_cap(cold, monkeypatch):
    """The doctor shows the local routes' recent usage; there is no session cap by default (owner decision
    2026-10-06), and an opt-in cap warns once a kind is used up."""
    for name in media_ledger.SESSION_ENV.values():
        monkeypatch.delenv(name, raising=False)
    ledger = media_ledger.Ledger(cold)
    for index in range(9):
        reservation = ledger.reserve({"jobDir": f"out/{index}", "fingerprint": f"{index:02d}" * 32, "provider": "openai",
                                      "model": "codex-image_gen", "kind": "image", "route": "codex-cli",
                                      "reservedUsd": 0.0}, session=media_ledger.session_limits())
        ledger.commit(reservation, status="done")
    report = forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8")
    entry = check(report, "media.ledger")
    assert entry["status"] == "OK" and "9 images, 0 videos (no session cap)" in entry["detail"]
    monkeypatch.setenv("FORGE_SESSION_IMAGES", "9")
    entry = check(forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8"), "media.ledger")
    assert entry["status"] == "WARN" and "session cap is reached for images" in entry["detail"]
    assert "9 of 9 images, 0 videos" in entry["detail"]
    monkeypatch.setenv("FORGE_SESSION_IMAGES", "lots")
    entry = check(forge_doctor.diagnose(project_dir=cold, run_versions=False, console_encoding="utf-8"), "media.ledger")
    assert entry["status"] == "WARN" and "FORGE_SESSION_IMAGES" in entry["detail"]
