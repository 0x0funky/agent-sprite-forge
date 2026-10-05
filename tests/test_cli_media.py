"""cli_media.py: opt-in Codex/Grok CLI routes, provenance, resume --adopt and adopt --codex-thread
(plan B22-T2, B22-T3; F-13 issue #4 / PR #5).

Offline: every CLI here is a fake from tests/fixtures/fake_cli, run with this interpreter, and
CODEX_HOME / GROK_HOME point at temporary folders. Nothing contacts a provider, no real CLI is
started and no credential file is read.

The skill's scripts import each other by their plain names (forge_doctor, media_ledger), so the
tests import them the same way to patch the instances the scripts use.
"""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from forge_testutils import FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, run_cli, script_path

SCRIPTS = SKILLS_DIR / "generate2dmedia" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import cli_media  # noqa: E402
import forge_doctor  # noqa: E402
import media_ledger  # noqa: E402

FAKES = FIXTURES_DIR / "fake_cli"
THREAD = "0199aa11-2222-7333-8444-955556666777"
SESSION = "01a0bb22-3333-7444-8555-a66667777888"
CANARIES = {"OPENAI_API_KEY": "sk-cli-canary-0123456789abcdef", "XAI_API_KEY": "xai-cli-canary-0123456789abcdef",
            "CODEX_API_KEY": "codex-cli-canary-0123456789ab", "GITHUB_TOKEN": "ghp_cli_canary_0123456789abcdef"}
# Schema request (handoff section 5): $defs/route_proofs_v1 for <project>/.forge/route-proofs.json.
ROUTE_PROOFS_V1 = {
    "description": "<project>/.forge/route-proofs.json written by cli_media.py: version-keyed evidence that an opt-in "
                   "CLI route works. A record is valid only for its route, native tool, CLI version and recipe; "
                   "forge_doctor.py reads it for the TOOL_EXPOSED and VERIFIED ladder steps.",
    "type": "object",
    "required": ["schema", "proofs"],
    "properties": {
        "schema": {"const": "generate2dmedia.route_proofs.v1"},
        "proofs": {"type": "array", "items": {
            "type": "object",
            "required": ["route", "tool", "version", "recipe", "level", "verifiedAt"],
            "properties": {
                "route": {"enum": ["codex-cli", "grok-cli", "grok-acp"]},
                "tool": {"enum": ["image_gen", "image_edit", "image_to_video"]},
                "version": {"type": "string", "minLength": 1},
                "versionText": {"type": "string", "minLength": 1},
                "recipe": {"type": "string", "pattern": "^[a-z0-9-]+/[0-9]+$"},
                "level": {"enum": ["TOOL_EXPOSED", "VERIFIED"]},
                "verifiedAt": {"$ref": "common.schema.json#/$defs/timestamp"},
                "runId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "method": {"type": "string"},
                "jobDir": {"$ref": "common.schema.json#/$defs/relPath"},
                "artifactSha256": {"$ref": "common.schema.json#/$defs/sha256"},
            },
            "if": {"properties": {"level": {"const": "VERIFIED"}}, "required": ["level"]},
            "then": {"required": ["artifactSha256"]},
        }},
    },
}


def patched_media_validator(name: str):
    """A validator for media.schema.json#/$defs/<name> with the section 5 additions applied in memory."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    folder = SKILLS_DIR / "generate2dmedia" / "references" / "schemas"
    schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.schema.json")}
    media = copy.deepcopy(schemas["media.schema.json"])
    media["$defs"]["route_proofs_v1"] = ROUTE_PROOFS_V1
    schemas["media.schema.json"] = media
    registry = Registry().with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in schemas.values())
    return Draft202012Validator({"$ref": f"{media['$id']}#/$defs/{name}"}, registry=registry)


def assert_media(document: dict, name: str) -> None:
    assert_valid_contract(document, "media", name, skill="generate2dmedia")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic CLI homes, the fake CLIs in place of the native ones, a project folder as cwd."""
    for name in (*CANARIES, "FORGE_MAX_PAID_REQUESTS", "FAKE_CLI_MODE", "FAKE_THREAD_ID", "FAKE_SESSION_ID",
                 "FAKE_CODEX_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    def fake(cli):
        path = FAKES / f"fake_{cli}.py"
        return cli_media.RouteCli([sys.executable, str(path)], forge_doctor.CliInfo(cli, path=path, source="test"))

    monkeypatch.setattr(cli_media, "resolve_route_cli", fake)
    (project / "prompt.txt").write_text("A small red apple icon on flat magenta.", encoding="utf-8")
    (project / "ref.png").write_bytes(forge_doctor._tiny_png(64))
    return SimpleNamespace(tmp=tmp_path, project=project, log=log)


def run(capsys, *argv):
    """cli_media.main in-process: (exit code, the one-line JSON on stdout or None, stderr)."""
    code = cli_media.main(list(argv))
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) <= 1, out
    assert out.isascii() and err.isascii()
    return code, json.loads(lines[0]) if lines else None, err


def entries(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def generations(log: Path) -> list[dict]:
    """Logged CLI invocations that would spend quota (anything but --version / login status)."""
    return [e for e in entries(log) if "argv" in e and e["argv"][:1] not in (["--version"], ["login"])]


def image_args(output="out/hero", route="codex-cli", *extra):
    return ["image", "--route", route, "--prompt-file", "prompt.txt", "--output-dir", output, *extra]


def ledger_states(project: Path) -> list[str]:
    return [state["status"] for state in media_ledger.Ledger(project).entries().values()]


def run_records(project: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((project / ".forge" / "cli-runs").glob("*.json"))]


# --------------------------------------------------------------------------- the three standard CLI tests

def test_help_works_under_cp1252():
    assert_cli_help("generate2dmedia", "cli_media")
    for verb in ("image", "edit", "video", "resume", "adopt", "batch"):
        result = run_cli([script_path("generate2dmedia", "cli_media"), verb, "--help"], "cp1252", timeout=120)
        assert result.returncode == 0 and result.stdout.isascii() and "--" in result.stdout, (verb, result.stderr)


def test_refuses_an_existing_output_dir(env, capsys):
    (env.project / "out" / "hero").mkdir(parents=True)
    code, _, err = run(capsys, *image_args(), "--execute")
    assert code == 1 and "OUTPUT_EXISTS" in err
    assert not generations(env.log) and not (env.project / ".forge" / "ledger.jsonl").exists()


@pytest.mark.parametrize("mode, error", [("bad_magic", "ARTIFACT_INVALID"), ("stale", "ARTIFACT_STALE"),
                                         ("wrong_path", "ARTIFACT_PATH_REJECTED"), ("unavailable", "TOOL_UNAVAILABLE"),
                                         ("two_images", "ARTIFACT_COUNT")])
def test_qc_failure_publishes_nothing(env, capsys, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    code, _, err = run(capsys, *image_args(), "--execute")
    assert code == 1 and error in err
    out = env.project / "out"
    assert not (out / "hero").exists()
    assert not list(out.glob(".hero.stage-*")) if out.exists() else True  # no stage left behind either
    assert ledger_states(env.project) == ["failed"]
    record, = run_records(env.project)
    assert record["status"] == "failed" and record["error"]["code"] == error
    assert_media(record, "job_v2")


# --------------------------------------------------------------------------- dry run and success paths

def test_dry_run_spawns_nothing(env, capsys, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must not start any process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    before = sorted(p.name for p in env.project.iterdir())
    plans = [run(capsys, *image_args())[1],
             run(capsys, "edit", "--route", "grok-cli", "--reference", "ref.png", "--prompt-file", "prompt.txt",
                 "--output-dir", "out/edit")[1],
             run(capsys, "video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt",
                 "--output-dir", "out/clip")[1]]
    jobs = env.project / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "command": "image", "route": "codex-cli",
                                          "prompt_file": "prompt.txt", "output_dir": "out/a"}]}), encoding="utf-8")
    code, batch, _ = run(capsys, "batch", "jobs.json")
    assert code == 0 and batch["execution"] == "dry-run" and batch["consent"][0]["consent"] == "quota"
    jobs.unlink()
    for plan in plans:
        assert plan["execution"] == "dry-run" and plan["consent"]["quota"] is True and plan["consent"]["calls"] == 1
        assert plan["estimate"]["usd"] == 0.0 and plan["cli"]["recipe"]
        assert any("no proof" in w for w in plan["warnings"])
    codex, grok, acp = (plan["command"] for plan in plans)
    assert {"--ignore-user-config", "--ephemeral", "read-only", "--disable", "shell_tool"} <= set(codex)
    assert {"--no-subagents", "--disable-web-search", "--deny", "Bash", "streaming-json", "image_edit"} <= set(grok)
    assert acp[1:3] == ["agent", "--no-leader"] and acp[-1] == "stdio"
    assert sorted(p.name for p in env.project.iterdir()) == before  # nothing written
    assert not env.log.exists()


def test_codex_success_has_provenance_ledger_and_proof(env, capsys, monkeypatch):
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)
    code, summary, err = run(capsys, *image_args(), "--purpose", "hero idle v1", "--execute")
    assert code == 0, err
    out = env.project / "out" / "hero"
    assert sorted(p.name for p in out.iterdir()) == ["generated.png", "job.json", "prompt.txt"]
    job = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert_media(job, "job_v2")
    assert job["route"] == "codex-cli" and job["status"] == "done" and job["artSource"] == "host_image"
    assert job["artifact"]["path"] == "generated.png" and job["artifact"]["sha256"] == summary["sha256"]
    assert forge_doctor.file_sha256(out / "generated.png") == summary["sha256"] == job["adoption"]["sourceSha256"]
    assert job["adoption"]["source"].startswith(f"CODEX_HOME/generated_images/{job['cliRun']['threadId']}/")
    assert job["adoption"]["finalAnswerNamesFile"] is True and job["receipt"]["purpose"] == "hero idle v1"
    assert job["ledger"]["projectDir"] == "../.." and job["cliRun"]["version"] == "0.155.1"
    lines = media_ledger.Ledger(env.project).lines()
    assert [line["status"] for line in lines] == ["reserved", "done"]
    assert all(line["route"] == "codex-cli" and line["quotaCall"] is True and line["reservedUsd"] == 0.0 for line in lines)
    for line in lines:
        assert_media(line, "ledger_line_v1")
    record, = run_records(env.project)
    assert_media(record, "job_v2")
    assert record["status"] == "done" and record["cliRun"]["threadId"] == job["cliRun"]["threadId"]
    assert (env.project / ".forge" / "cli-runs" / f"{record['cliRun']['runId']}.prompt.txt").is_file()
    proofs = json.loads((env.project / ".forge" / "route-proofs.json").read_text(encoding="utf-8"))
    patched_media_validator("route_proofs_v1").validate(proofs)
    proof, = proofs["proofs"]
    assert (proof["route"], proof["level"], proof["version"]) == ("codex-cli", "VERIFIED", "0.155.1")
    assert proof["artifactSha256"] == summary["sha256"] and proof["jobDir"] == "out/hero"
    exec_entry, = generations(env.log)
    assert exec_entry["credentialEnv"] == []  # API keys and other credentials never reach the CLI
    assert {"--sandbox", "read-only", "--ephemeral", "--json"} <= set(exec_entry["argv"])
    texts = [json.dumps(summary), err, (out / "job.json").read_text(encoding="utf-8"),
             (env.project / ".forge" / "ledger.jsonl").read_text(encoding="utf-8"), json.dumps(record),
             (env.project / ".forge" / "route-proofs.json").read_text(encoding="utf-8")]
    for secret in CANARIES.values():
        assert not any(secret in text for text in texts)


def test_grok_image_edit_and_acp_video_succeed(env, capsys):
    reference = forge_doctor.file_sha256(env.project / "ref.png")
    results = {
        "image": run(capsys, *image_args("out/gen", "grok-cli"), "--execute"),
        "edit": run(capsys, "edit", "--route", "grok-cli", "--reference", "ref.png", "--prompt-file", "prompt.txt",
                    "--output-dir", "out/edit", "--execute"),
        "video": run(capsys, "video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt",
                     "--duration", "4", "--resolution", "480p", "--output-dir", "out/clip", "--execute"),
    }
    for verb, (code, summary, err) in results.items():
        assert code == 0, (verb, err)
        job = json.loads(Path(summary["metadata"]).read_text(encoding="utf-8"))
        assert_media(job, "job_v2")
        assert job["adoption"]["source"].startswith("GROK_HOME/sessions/*/" + job["cliRun"]["sessionId"])
        assert "%3A" not in json.dumps(job) and "Users" not in job["adoption"]["source"]  # no absolute paths
    video = json.loads((env.project / "out" / "clip" / "job.json").read_text(encoding="utf-8"))
    assert video["artifact"]["path"] == "generated.mp4" and video["options"] == {
        "tool": "image_to_video", "duration": 4, "resolution": "480p"}
    assert video["references"][0]["sha256"] == reference
    log = entries(env.log)
    assert {"outcome": "selected", "optionId": "allow-once"} in [e.get("permission") for e in log]
    assert next(e for e in log if e.get("args"))["args"]["duration"] == 4
    starts = [e for e in log if e.get("cli") == "grok" and "argv" in e]
    assert all(e["isolation"] == {"GROK_DISABLE_AUTOUPDATER": "true", "GROK_MEMORY": "false", "GROK_SUBAGENTS": "0"}
               for e in starts)
    assert all(e["credentialEnv"] == [] for e in starts)
    proofs = json.loads((env.project / ".forge" / "route-proofs.json").read_text(encoding="utf-8"))
    patched_media_validator("route_proofs_v1").validate(proofs)
    assert {(p["route"], p["tool"], p["level"]) for p in proofs["proofs"]} == {
        ("grok-cli", "image_gen", "VERIFIED"), ("grok-cli", "image_edit", "VERIFIED"),
        ("grok-acp", "image_to_video", "VERIFIED")}


# --------------------------------------------------------------------------- kill switches and typed errors

@pytest.mark.parametrize("argv, mode, error", [
    (image_args(), "unexpected_tool", "UNEXPECTED_TOOL"),
    (image_args(), "second_image_item", "TOOL_LIMIT"),
    (image_args(route="grok-cli"), "unexpected_tool", "UNEXPECTED_TOOL"),
    (image_args(route="grok-cli"), "two_calls", "TOOL_LIMIT"),
    (["video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt", "--output-dir",
      "out/hero"], "unexpected_tool", "UNEXPECTED_TOOL"),
    (["video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt", "--output-dir",
      "out/hero"], "mismatch", "PERMISSION_MISMATCH"),
])
def test_unexpected_tool_calls_kill_the_run(env, capsys, argv, mode, error):
    (env.project / "prompt.txt").write_text(f"A slime idles. [[fake:{mode}]]", encoding="utf-8")
    started = time.monotonic()
    code, _, err = run(capsys, *argv, "--execute", "--timeout", "45")
    assert code == 1 and error in err
    assert time.monotonic() - started < 30  # killed at once, not after the fake's 60 s sleep or the timeout
    assert not (env.project / "out" / "hero").exists()
    record, = run_records(env.project)
    assert record["error"]["code"] == error and record["status"] == "failed"
    assert ledger_states(env.project) == ["failed"]
    granted = [e["permission"] for e in entries(env.log) if e.get("permission", {}).get("outcome") == "selected"]
    assert not granted  # a mismatched or unexpected call is never allowed


def test_timeout_kills_and_keeps_the_run_id(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_MODE", "timeout")
    monkeypatch.setenv("FAKE_THREAD_ID", THREAD)
    started = time.monotonic()
    code, _, err = run(capsys, *image_args(), "--execute", "--timeout", "2")
    assert code == 1 and "TIMEOUT" in err and time.monotonic() - started < 20
    record, = run_records(env.project)
    assert record["status"] == "submit_unknown" and record["cliRun"]["threadId"] == THREAD
    assert ledger_states(env.project) == ["unknown"]  # quota may have been spent: held until settled
    assert_media(record, "job_v2")


@pytest.mark.parametrize("mode, error", [("auth", "AUTH_REQUIRED"), ("rate", "RATE_LIMIT"), ("flood", "OUTPUT_LIMIT")])
def test_cli_failures_are_typed(env, capsys, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    code, _, err = run(capsys, *image_args(), "--execute", "--timeout", "45")
    assert code == 1 and error in err
    assert not (env.project / "out" / "hero").exists()


def test_acp_auth_error_and_refused_client_requests(env, capsys, monkeypatch):
    video = ["video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt"]
    monkeypatch.setenv("FAKE_CLI_MODE", "auth")
    code, _, err = run(capsys, *video, "--output-dir", "out/a", "--execute")
    assert code == 1 and "AUTH_REQUIRED" in err
    monkeypatch.setenv("FAKE_CLI_MODE", "fs_request")
    code, summary, err = run(capsys, *video, "--output-dir", "out/b", "--execute")
    assert code == 0, err
    assert -32601 in [e.get("fsAnswer") for e in entries(env.log)]  # the client offers no file system


# --------------------------------------------------------------------------- batch, caps and duplicates

def test_auth_error_stops_the_batch(env, capsys):
    prompts = {"a": "A red apple.", "b": "A blue pear. [[fake:auth]]", "c": "A green plum."}
    for name, text in prompts.items():
        (env.project / f"{name}.txt").write_text(text, encoding="utf-8")
    jobs = [{"id": name, "command": "image", "route": "codex-cli", "prompt_file": f"{name}.txt",
             "output_dir": f"out/{name}"} for name in prompts]
    (env.project / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    code, _, err = run(capsys, "batch", "jobs.json", "--execute")
    assert code == 1 and "NOT_VERIFIED" in err and not generations(env.log)  # unverified routes need opting in
    code, summary, err = run(capsys, "batch", "jobs.json", "--execute", "--allow-unverified")
    assert code == 1 and "stopped at b: AUTH_REQUIRED" in err
    assert [(r["id"], r["status"], r["outcomeCode"]) for r in summary["results"]] == [
        ("a", "generated", "ok"), ("b", "failed", "AUTH_REQUIRED")]
    assert summary["remaining"] == ["c"] and summary["stopped"] and not summary["complete"]
    assert len(generations(env.log)) == 2  # c was never dispatched and nothing was retried
    progress = json.loads((env.project / "jobs.progress.json").read_text(encoding="utf-8"))
    assert_media(progress, "batch_progress_v1")
    assert (env.project / "out" / "a" / "generated.png").is_file() and not (env.project / "out" / "c").exists()


def test_duplicate_guard_and_call_cap(env, capsys):
    assert run(capsys, *image_args("out/one"), "--execute")[0] == 0
    code, _, err = run(capsys, *image_args("out/two"), "--execute")
    assert code == 1 and "DUPLICATE" in err and not (env.project / "out" / "two").exists()
    assert run(capsys, *image_args("out/two"), "--execute", "--allow-duplicate")[0] == 0
    code, _, err = run(capsys, *image_args("out/three"), "--execute", "--allow-duplicate", "--max-calls", "2")
    assert code == 1 and "CAP" in err
    assert len(generations(env.log)) == 2 and ledger_states(env.project) == ["done", "done"]


# --------------------------------------------------------------------------- provenance: resume and adopt

def test_resume_adopts_without_rerunning(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_MODE", "hang_after_image")
    code, _, err = run(capsys, *image_args(), "--execute", "--timeout", "3")
    assert code == 1 and "TIMEOUT" in err and not (env.project / "out" / "hero").exists()
    record, = run_records(env.project)
    run_id = record["cliRun"]["runId"]
    assert ledger_states(env.project) == ["unknown"]
    code, inspection, _ = run(capsys, "resume", "--run", run_id)
    assert code == 0 and inspection["adoptable"] is True and not (env.project / "out" / "hero").exists()
    code, adopted, err = run(capsys, "resume", "--run", run_id, "--adopt")
    assert code == 0 and adopted["adopted"] is True, err
    out = env.project / "out" / "hero"
    job = json.loads((out / "job.json").read_text(encoding="utf-8"))
    assert_media(job, "job_v2")
    assert job["adoption"]["method"].endswith("(resume --adopt)") and job["receipt"]["outcomeCode"] == "adopted"
    assert (out / "prompt.txt").read_text(encoding="utf-8").startswith("A small red apple")
    assert forge_doctor.file_sha256(out / "generated.png") == adopted["sha256"]
    assert ledger_states(env.project) == ["done"] and len(generations(env.log)) == 1  # the CLI never ran again
    final, = run_records(env.project)
    assert final["status"] == "done"
    assert_media(final, "job_v2")
    assert run(capsys, "resume", "--run", run_id, "--adopt")[1]["adopted"] is False  # idempotent


def test_grok_resume_finds_the_output_without_the_end_event(env, capsys, monkeypatch):
    """Killed before Grok's end event: the record holds only the masked output path, which names the session."""
    monkeypatch.setenv("FAKE_CLI_MODE", "hang_after_tool")
    monkeypatch.setenv("FAKE_SESSION_ID", SESSION)
    code, _, err = run(capsys, *image_args(route="grok-cli"), "--execute", "--timeout", "3")
    assert code == 1 and "TIMEOUT" in err
    record, = run_records(env.project)
    assert "sessionId" not in record["cliRun"] and record["cliRun"]["sourceRel"] == f"*/{SESSION}/images/1.png"
    code, adopted, err = run(capsys, "resume", "--run", record["cliRun"]["runId"], "--adopt", "--output-dir", "out/kept")
    assert code == 0, err
    job = json.loads(Path(adopted["metadata"]).read_text(encoding="utf-8"))
    assert_media(job, "job_v2")
    assert job["adoption"]["source"] == f"GROK_HOME/sessions/*/{SESSION}/images/1.png"
    assert len(generations(env.log)) == 1 and ledger_states(env.project) == ["done"]


def test_unclassified_cli_errors_keep_a_scrubbed_clue(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", CANARIES["XAI_API_KEY"])
    text = f"panic: unknown flag --disable sleep_tool\nat {Path.home() / 'x'}\nkey {CANARIES['XAI_API_KEY']}"
    message = cli_media._failure_message(cli_media.classify_failure(text), "codex", text)
    assert "unknown flag --disable sleep_tool" in message and "~" in message
    assert CANARIES["XAI_API_KEY"] not in message and str(Path.home()) not in message


def _dir_link(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without the privilege a junction; skip if neither works."""
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, OSError, AttributeError):
            pass
    pytest.skip("cannot create a symlink or junction here")


def test_symlinked_output_folder_is_rejected(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_THREAD_ID", THREAD)
    _dir_link(env.tmp / "codex-home" / "generated_images" / THREAD, env.tmp / "elsewhere")
    code, _, err = run(capsys, *image_args(), "--execute")
    assert code == 1 and "ARTIFACT_PATH_REJECTED" in err and not (env.project / "out" / "hero").exists()
    assert list((env.tmp / "elsewhere").glob("*.png"))  # the image existed, behind the link
    code, _, err = run(capsys, "adopt", "--codex-thread", THREAD, "--output-dir", "out/adopted")
    assert code == 1 and "ARTIFACT_PATH_REJECTED" in err and not (env.project / "out" / "adopted").exists()


def test_symlinked_image_file_is_rejected(env, capsys):
    folder = env.tmp / "codex-home" / "generated_images" / THREAD
    folder.mkdir(parents=True)
    outside = env.tmp / "outside.png"
    outside.write_bytes(forge_doctor._tiny_png(64))
    try:
        os.symlink(outside, folder / "exec-link.png")
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks need a privilege on this machine")
    code, _, err = run(capsys, "adopt", "--codex-thread", THREAD, "--output-dir", "out/adopted")
    assert code == 1 and "ARTIFACT_PATH_REJECTED" in err


def test_two_images_are_rejected_unless_one_is_chosen(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_MODE", "two_images")
    monkeypatch.setenv("FAKE_THREAD_ID", THREAD)
    code, _, err = run(capsys, *image_args(), "--execute")
    assert code == 1 and "ARTIFACT_COUNT" in err and not (env.project / "out" / "hero").exists()
    code, _, err = run(capsys, "adopt", "--codex-thread", THREAD, "--output-dir", "out/adopted")
    assert code == 1 and "ARTIFACT_COUNT" in err and "--file" in err
    name = sorted((env.tmp / "codex-home" / "generated_images" / THREAD).glob("*.png"))[1].name
    lines = media_ledger.Ledger(env.project).lines()
    code, adopted, err = run(capsys, "adopt", "--codex-thread", THREAD, "--file", name, "--output-dir", "out/adopted")
    assert code == 0, err
    job = json.loads(Path(adopted["metadata"]).read_text(encoding="utf-8"))
    assert_media(job, "job_v2")
    assert job["adoption"]["method"] == "codex-thread-folder" and job["adoption"]["threadId"] == THREAD
    assert job["adoption"]["source"].endswith("/" + name)
    assert media_ledger.Ledger(env.project).lines() == lines  # adopting spends nothing and records no call


def _rollout(home: Path, thread: str, records: list[dict]) -> Path:
    path = home / "sessions" / "2026" / "10" / "05" / f"rollout-2026-10-05T01-02-03-{thread}.jsonl"
    path.parent.mkdir(parents=True)
    lines = [{"type": "session_meta", "payload": {"id": thread}}, *records,
             {"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}}]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_adopt_codex_thread_from_the_session_rollout(env, capsys):
    """Codex Desktop keeps no PNG (issue #4): the image is inline in the session rollout."""
    image = base64.b64encode(forge_doctor._tiny_png(64)).decode("ascii")
    other = base64.b64encode(forge_doctor._tiny_png(96)).decode("ascii")
    _rollout(env.tmp / "codex-home", THREAD, [
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "Extension", "kind": "image_gen.generation", "status": "completed", "result": image}}},
        {"type": "event_msg", "payload": {"type": "image_generation_end", "status": "completed", "result": other}}])
    code, _, err = run(capsys, "adopt", "--codex-thread", THREAD, "--output-dir", "out/a")
    assert code == 1 and "ARTIFACT_COUNT" in err and "--index" in err
    code, adopted, err = run(capsys, "adopt", "--codex-thread", THREAD, "--index", "2", "--output-dir", "out/a")
    assert code == 0, err
    assert (env.project / "out" / "a" / "generated.png").read_bytes() == forge_doctor._tiny_png(96)
    job = json.loads(Path(adopted["metadata"]).read_text(encoding="utf-8"))
    assert_media(job, "job_v2")
    assert job["adoption"]["method"] == "codex-thread-rollout"
    code, _, err = run(capsys, "adopt", "--codex-thread", "no-such-thread-0000", "--output-dir", "out/b")
    assert code == 1 and "ARTIFACT_MISSING" in err


def test_invalid_requests_fail_before_anything_runs(env, capsys):
    cases = [
        (image_args("out/x", "codex-cli", "--timeout", "0.5"), "--timeout"),
        (["video", "--route", "grok-acp", "--reference", "prompt.txt", "--prompt-file", "prompt.txt",
          "--output-dir", "out/x"], "PNG, JPEG or WebP"),
        (["video", "--route", "grok-acp", "--reference", "ref.png", "--prompt-file", "prompt.txt", "--duration",
          "30", "--output-dir", "out/x"], "--duration"),
    ]
    (env.project / "empty.txt").write_text("   ", encoding="utf-8")
    cases.append((["image", "--route", "codex-cli", "--prompt-file", "empty.txt", "--output-dir", "out/x"], "prompt"))
    for argv, message in cases:
        code, _, err = run(capsys, *argv, "--execute")
        assert code == 1 and "INVALID_REQUEST" in err and message in err, err
    assert not env.log.exists() and not (env.project / ".forge").exists()
