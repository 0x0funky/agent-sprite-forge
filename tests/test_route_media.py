"""route_media.py: the one entry point for generated images and clips (owner decision 2026-10-06).

Offline: API calls go to fake transports, local routes to the fake Codex and Grok CLIs of
tests/fixtures/fake_cli, and the user config folder (APPDATA / XDG_CONFIG_HOME) and the CLI homes
point at temporary folders, so no real key, config file, credential or CLI is ever read or run.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from forge_testutils import FIXTURES_DIR, SKILLS_DIR, assert_cli_help, assert_valid_contract, run_cli, script_path

SCRIPTS = SKILLS_DIR / "generate2dmedia" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import cli_media  # noqa: E402  (the instances route_media itself imports)
import forge_doctor  # noqa: E402
import media_config  # noqa: E402
import media_ledger  # noqa: E402
import route_media  # noqa: E402

media = route_media.generate_media
FAKES = FIXTURES_DIR / "fake_cli"
OPENAI_KEY = "sk-route-canary-" + "0123456789abcdef" * 2
XAI_KEY = "xai-route-canary-" + "fedcba9876543210" * 2
MP4 = b"\x00\x00\x00\x18ftypmp42data"


def png(size=(32, 48), colour=(255, 0, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, "PNG")
    return output.getvalue()


def b64_image() -> dict:
    return {"data": [{"b64_json": base64.b64encode(png((64, 64), (30, 100, 180))).decode()}]}


class Fake:
    """API transport stand-in: records method, URL, key and body; answers from a list."""

    def __init__(self, results=(), download=MP4):
        self.results = iter(results)
        self.calls = []
        self.content = download

    def api(self, method, url, key, body=None, content_type=None, timeout=90, meta=None):
        self.calls.append(SimpleNamespace(method=method, url=url, key=key, body=body, content_type=content_type))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def download(self, url, timeout=90):
        return self.content


class Refuse:
    def api(self, *a, **k):
        raise AssertionError("network used")

    download = api


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project as cwd, an empty user config folder, no keys, no caps, no real CLI."""
    for name in (*media_config.KEY_VARIABLES, "CODEX_API_KEY", media_ledger.MAX_PAID_ENV, route_media.FAKE_ENV,
                 "FAKE_CLI_MODE", "FAKE_THREAD_ID", "FAKE_SESSION_ID", "FAKE_CODEX_VERSION", "FAKE_GROK_VERSION",
                 "FAKE_GROK_DURATIONS", "FAKE_TOOL_ERROR", *media_ledger.SESSION_ENV.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    (project / "prompt.txt").write_text("One square 1024x1024 image of a fox knight on flat #FF00FF.", encoding="utf-8")
    (project / "first.png").write_bytes(png())
    (project / "peer.png").write_bytes(png(colour=(10, 200, 10)))
    installed = {"codex": False, "grok": False}

    def fake(cli):
        if not installed[cli]:
            return cli_media.RouteCli([], forge_doctor.CliInfo(cli, problem="not found on PATH"))
        path = FAKES / f"fake_{cli}.py"
        return cli_media.RouteCli([sys.executable, str(path)], forge_doctor.CliInfo(cli, path=path, source="test"))

    monkeypatch.setattr(cli_media, "resolve_route_cli", fake)
    return SimpleNamespace(tmp=tmp_path, project=project, log=log, installed=installed)


def configure(**fields) -> Path:
    """Write the user config file where media_config looks for it (inside the test's temp folders)."""
    path = media_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def run(capsys, *argv, transport=None):
    """route_media.main in-process: (exit code, the one-line JSON on stdout or None, stderr)."""
    code = route_media.main(list(argv), transport=transport)
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) <= 1, out
    assert out.isascii() and err.isascii()
    return code, json.loads(lines[0]) if lines else None, err


def fake_log(log: Path) -> list:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def generations(log: Path) -> list:
    return [e for e in fake_log(log) if "argv" in e and e["argv"][:1] not in (["--version"], ["login"])]


def ledger(project: Path) -> list:
    return list(media_ledger.Ledger(project).entries().values())


# --------------------------------------------------------------------------- the CLI contract

def test_help_works_under_cp1252():
    assert_cli_help("generate2dmedia", "route_media")
    for verb in ("image", "video", "resolve"):
        result = run_cli([script_path("generate2dmedia", "route_media"), verb, "--help"], "cp1252", timeout=120)
        assert result.returncode == 0 and result.stdout.isascii() and "--" in result.stdout, (verb, result.stderr)


def test_config_path_is_user_level(tmp_path, monkeypatch):
    """%APPDATA%\\agent-sprite-forge\\config.json on Windows, $XDG_CONFIG_HOME (default ~/.config) elsewhere."""
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        assert media_config.config_path() == tmp_path / "roaming" / "agent-sprite-forge" / "config.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert media_config.config_path() == tmp_path / "xdg" / "agent-sprite-forge" / "config.json"
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")
        assert media_config.config_path() == Path.home() / ".config" / "agent-sprite-forge" / "config.json"


def test_keys_environment_first_then_config_and_models(env, monkeypatch):
    none = dict.fromkeys(media_config.PROVIDER_KEYS, False)
    assert media_config.configured() == none
    path = configure(OPENAI_API_KEY=f"  {OPENAI_KEY}  ", XAI_API_KEY="", models={"xai-video": "grok-imagine-video-1.5-lite",
                                                                              "openai-image": "bad model id!"})
    assert media_config.api_key("openai") == OPENAI_KEY and media_config.key_source("openai") == "config"
    assert media_config.api_key("xai") is None and media_config.configured() == {**none, "openai": True}
    assert media_config.model_for("xai-video") == "grok-imagine-video-1.5-lite"
    assert media_config.model_for("openai-image") == "gpt-image-2.5-sunburst"  # an implausible id is ignored
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment-0123456789")
    assert media_config.api_key("openai") == "sk-from-the-environment-0123456789"
    assert media_config.key_source("openai") == "environment"
    assert set(media_config.known_secrets()) == {"sk-from-the-environment-0123456789", OPENAI_KEY}
    path.write_text("{not json " + OPENAI_KEY, encoding="utf-8")
    settings, problem = media_config.load_config()
    assert settings == {} and "not valid" in problem and OPENAI_KEY not in problem


# --------------------------------------------------------------------------- resolution

def test_resolve_follows_the_owner_order(env, capsys, monkeypatch):
    code, result, _ = run(capsys, "resolve", "--kind", "image")
    assert code == route_media.NO_ROUTE_EXIT == 3
    assert result["status"] == "no-route" and result["fallback"] == "codeart2d" and len(result["skipped"]) == 7
    env.installed.update(codex=True, grok=True)
    code, result, _ = run(capsys, "resolve", "--kind", "image")
    assert code == 0 and result["route"] == "local:codex-cli" and result["label"] == "Codex (local CLI)"
    assert result["order"] == ["api:openai", "api:gemini", "api:xai", "api:byteplus", "api:fal", "local:codex-cli",
                               "local:grok-cli"]
    configure(XAI_API_KEY=XAI_KEY)
    code, result, _ = run(capsys, "resolve", "--kind", "image")
    assert (result["route"], result["model"]) == ("api:xai", "grok-imagine-image-2.0")
    assert result["available"] == ["api:xai", "local:codex-cli", "local:grok-cli"]
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    code, result, _ = run(capsys, "resolve", "--kind", "image")
    assert (result["route"], result["model"]) == ("api:openai", "gpt-image-2.5-sunburst")
    # Two references: Grok (local CLI) takes one, so it drops out; xAI grok-imagine-image-2.0 takes up to 5.
    code, result, _ = run(capsys, "resolve", "--kind", "image", "--references", "2")
    assert result["available"] == ["api:openai", "api:xai", "local:codex-cli"]
    code, result, _ = run(capsys, "resolve", "--kind", "image", "--references", "6")
    assert result["available"] == ["api:openai", "local:codex-cli"]
    assert "api:xai: grok-imagine-image-2.0 takes at most 5 reference images" in result["skipped"]
    code, result, _ = run(capsys, "resolve", "--kind", "video")
    assert result["route"] == "api:xai" and result["pinsLastFrame"] is True
    code, result, _ = run(capsys, "resolve", "--kind", "video", "--route", "local", "--resolution", "1080p")
    assert code == 3 and "480p or 720p" in result["skipped"][0]
    code, result, _ = run(capsys, "resolve", "--kind", "image", "--route", "grok-cli")
    assert code == 0 and result["route"] == "local:grok-cli" and result["order"] == ["local:grok-cli"]
    with pytest.raises(SystemExit) as usage:
        route_media.main(["resolve", "--kind", "video", "--route", "codex-cli"])
    assert usage.value.code == 2
    for text in (json.dumps(result), capsys.readouterr().err):
        assert OPENAI_KEY not in text and XAI_KEY not in text


def test_xai_size_mapping():
    assert route_media.xai_image_options("1024x1024", 0) == ["--resolution", "1k", "--aspect-ratio", "1:1"]
    assert route_media.xai_image_options("1536x1024", 0) == ["--resolution", "2k", "--aspect-ratio", "3:2"]
    assert route_media.xai_image_options("1920x1080", 1) == ["--resolution", "2k"]  # an edit keeps its reference's shape
    assert route_media.xai_image_options("auto", 0) == []


# --------------------------------------------------------------------------- API routes: the key is the consent

def test_api_image_is_sent_at_once_with_the_configured_key(env, capsys):
    configure(OPENAI_API_KEY=OPENAI_KEY)
    transport = Fake([b64_image()])
    code, result, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "first.png", "--reference",
                            "peer.png", "--out-dir", "out/master", transport=transport)
    assert code == 0, err
    assert list(result)[:5] == ["status", "route", "artifact", "sha256", "estimateUsd"]
    assert (result["status"], result["route"], result["estimateUsd"]) == ("ok", "api:openai", None)  # unpriced model
    artifact = Path(result["artifact"])
    assert artifact == Path("out/master/generated.png") and artifact.is_file()
    assert forge_doctor.file_sha256(artifact) == result["sha256"]
    call, = transport.calls
    assert call.key == OPENAI_KEY and call.url.endswith("/images/edits") and call.body.count(b'name="image[]"') == 2
    assert b'name="size"\r\n\r\n1024x1024' in call.body
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert_valid_contract(job, "media", "job_v2", skill="generate2dmedia")
    assert job["status"] == "done" and job["receipt"]["purpose"] == "route_media image"
    line, = ledger(env.project)
    assert (line["status"], line["route"], line["provider"]) == ("done", "rest", "openai")
    # The same request again is a new take in a new folder, not a refused duplicate.
    code, again, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "first.png", "--reference",
                           "peer.png", "--out-dir", "out/master-2", transport=Fake([b64_image()]))
    assert code == 0 and again["route"] == "api:openai", err
    assert json.loads(Path(again["job"]).read_text(encoding="utf-8"))["receipt"]["attempt"] == 2
    texts = [json.dumps(result), err, Path(result["job"]).read_text(encoding="utf-8"),
             (env.project / ".forge" / "ledger.jsonl").read_text(encoding="utf-8")]
    assert not any(OPENAI_KEY in text for text in texts)
    assert "OPENAI_API_KEY" not in os.environ  # read in-process, never exported


def test_api_video_pins_the_last_frame_when_the_model_can(env, capsys, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    transport = Fake([{"request_id": "req-1"}, {"status": "done", "video": {"url": "https://media.example/v.mp4"}}])
    code, result, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--last-frame",
                            "first.png", "--out-dir", "out/idle", transport=transport)
    assert code == 0, err
    assert (result["route"], result["model"], result["lastFrameUsed"]) == ("api:xai", "grok-imagine-video-1.5", True)
    assert result["estimateUsd"] == pytest.approx(6 * 0.14 + 2 * 0.01) and Path(result["artifact"]).name == "generated.mp4"
    body = json.loads(transport.calls[0].body)
    assert body["duration"] == 6 and body["resolution"] == "720p" and body["image"] == body["last_frame"]
    configure(models={"xai-video": "grok-imagine-video-1.5-lite"})  # a model that cannot pin a last frame
    transport = Fake([{"request_id": "req-2"}, {"status": "done", "video": {"url": "https://media.example/v.mp4"}}])
    code, result, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--last-frame",
                            "first.png", "--out-dir", "out/idle-lite", transport=transport)
    assert code == 0 and result["lastFrameUsed"] is False and "not pinned" in result["notes"][0], err
    assert "last_frame" not in json.loads(transport.calls[0].body)


def test_an_account_refusal_passes_the_request_to_the_next_route(env, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    configure(XAI_API_KEY=XAI_KEY)
    quota = media.MediaError("API HTTP 429 insufficient_quota: You exceeded your current quota", code="quota", sent=True)
    transport = Fake([quota, b64_image()])
    code, result, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/prop", transport=transport)
    assert code == 0, err
    assert result["route"] == "api:xai" and [c.key for c in transport.calls] == [OPENAI_KEY, XAI_KEY]
    attempt, = result["attempts"]
    assert (attempt["route"], attempt["code"], attempt["keptIn"]) == ("api:openai", "quota", "prop.failed-api-openai")
    kept = json.loads((env.project / "out" / "prop.failed-api-openai" / "job.json").read_text(encoding="utf-8"))
    assert kept["status"] == "failed" and kept["receipt"]["outcomeCode"] == "quota"
    assert sorted(s["status"] for s in ledger(env.project)) == ["done", "failed"]
    assert (env.project / "out" / "prop" / "generated.png").is_file()


def test_an_accepted_video_job_is_never_generated_twice(env, capsys, monkeypatch):
    """xAI accepted the clip, then polling hit a rate limit: the job may still finish and be charged, so the
    request does not pass to Grok (local CLI); the folder stays where resume finds it."""
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    env.installed["grok"] = True
    limited = media.MediaError("API HTTP 429 rate_limit_exceeded: Rate limit reached", code="rate_limit", sent=True)
    transport = Fake([{"request_id": "req-accepted"}, limited])
    code, result, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir",
                            "out/walk", transport=transport)
    assert code == 1 and result is None and err.startswith("error: api:xai: rate_limit:")
    job = json.loads((env.project / "out" / "walk" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "interrupted" and job["requestId"] == "req-accepted"
    assert not generations(env.log) and not list((env.project / "out").glob("walk.failed-*"))
    assert [s["status"] for s in ledger(env.project)] == ["reserved"]  # still held: resume settles it


def test_a_damaged_config_file_is_reported_not_used(env, capsys):
    path = configure()
    path.write_text("{oops " + OPENAI_KEY, encoding="utf-8")
    env.installed["codex"] = True
    code, result, err = run(capsys, "resolve", "--kind", "image")
    assert code == 0 and result["route"] == "local:codex-cli"
    assert result["skipped"][0].startswith("user config file ignored: the user config file is not valid")
    assert OPENAI_KEY not in json.dumps(result) and OPENAI_KEY not in err


def test_a_request_level_failure_stops(env, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    refused = media.MediaError("API HTTP 400 moderation_blocked: rejected by the safety system", code="moderation",
                               sent=True)
    transport = Fake([refused])
    code, result, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/a", transport=transport)
    assert code == 1 and result is None and err.startswith("error: api:openai: moderation:")
    assert len(transport.calls) == 1  # xAI is never asked to draw what OpenAI refused
    code, _, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/a", transport=Refuse())
    assert code == 1 and "already exists" in err  # checked before any route


def test_dry_run_sends_and_writes_nothing(env, capsys, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    code, plan, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir",
                          "out/run", "--duration", "4", "--dry-run", transport=Refuse())
    assert code == 0, err
    assert plan["status"] == "dry-run" and plan["route"] == "api:xai" and plan["estimateUsd"] == pytest.approx(0.57)
    assert "output_second" in plan["estimate"]
    # The API keeps its own lengths (capabilities.json: xAI 1..15 s); only Grok (local CLI) is held to 6 or 10 s.
    assert (plan["durationRequested"], plan["durationUsed"]) == (4, 4) and "duration" not in plan
    assert sorted(p.name for p in env.project.iterdir()) == ["first.png", "peer.png", "prompt.txt"]


# --------------------------------------------------------------------------- local routes

def test_local_codex_route_attaches_references_and_records_the_proof(env, capsys):
    env.installed["codex"] = True
    code, result, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "first.png", "--reference",
                            "peer.png", "--out-dir", "out/master")
    assert code == 0, err
    assert (result["route"], result["model"], result["estimateUsd"]) == ("local:codex-cli", "codex-image_gen", 0.0)
    assert result["verifiedBefore"] is False and Path(result["artifact"]).is_file()
    run_entry, = generations(env.log)
    attached = [run_entry["argv"][i + 1] for i, a in enumerate(run_entry["argv"]) if a == "--image"]
    assert [Path(p).name for p in attached] == ["reference-1.png", "reference-2.png"]
    assert run_entry["credentialEnv"] == []
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert_valid_contract(job, "media", "job_v2", skill="generate2dmedia")
    assert [r["sha256"] for r in job["references"]] == [forge_doctor.file_sha256(env.project / n)
                                                        for n in ("first.png", "peer.png")]
    proofs = json.loads((env.project / ".forge" / "route-proofs.json").read_text(encoding="utf-8"))
    assert [(p["route"], p["level"]) for p in proofs["proofs"]] == [("codex-cli", "VERIFIED")]  # first use records it


def test_local_fallthrough_and_no_route_for_video(env, capsys, monkeypatch, tmp_path):
    """Codex is installed but not signed in: AUTH_REQUIRED passes the image to Grok (local CLI)."""
    signed_out = tmp_path / "signed_out_codex.py"
    signed_out.write_text("import sys\nif sys.argv[1:2] == ['--version']:\n    print('codex-cli 0.155.1')\n"
                          "    sys.exit(0)\nsys.stdin.read()\nprint('Error: 401 Unauthorized: please sign in', "
                          "file=sys.stderr)\nsys.exit(1)\n", encoding="utf-8")
    real = cli_media.resolve_route_cli  # the fixture's fakes
    env.installed["grok"] = True

    def clis(cli):
        if cli == "codex":
            return cli_media.RouteCli([sys.executable, str(signed_out)], forge_doctor.CliInfo(cli, path=signed_out,
                                                                                               source="test"))
        return real(cli)

    monkeypatch.setattr(cli_media, "resolve_route_cli", clis)
    code, result, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir",
                            "out/edit")
    assert code == 0, err
    assert (result["route"], result["model"]) == ("local:grok-cli", "grok-image_edit")
    assert [(a["route"], a["code"]) for a in result["attempts"]] == [("local:codex-cli", "AUTH_REQUIRED")]
    assert "keptIn" not in result["attempts"][0]  # a failed local run publishes nothing
    env.installed.update(codex=True, grok=False)
    monkeypatch.setattr(cli_media, "resolve_route_cli", real)
    code, result, _ = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir", "out/v")
    assert code == 3 and result == {"status": "no-route", "fallback": "codeart2d", "kind": "video", "requested": "auto",
                                    "skipped": ["api:xai: no XAI_API_KEY in the environment or the user config file",
                                                "api:byteplus: no ARK_API_KEY in the environment or the user config file",
                                                "api:fal: no FAL_KEY in the environment or the user config file",
                                                "local:grok-acp: Grok Build CLI is not installed (not found on "
                                                "PATH)"]}


def test_local_video_through_grok_acp(env, capsys):
    """Grok (local CLI) takes the first frame only and renders 6 or 10 s (live run 2026-10-06: a 4 s request failed
    as GENERATION_FAILED): the result says lastFrameUsed false, durationRequested and durationUsed, with notes."""
    env.installed["grok"] = True
    code, result, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--last-frame",
                            "first.png", "--duration", "4", "--resolution", "480p", "--out-dir", "out/run")
    assert code == 0, err
    assert result["route"] == "local:grok-acp" and result["lastFrameUsed"] is False
    assert (result["durationRequested"], result["durationUsed"], result["duration"]) == (4, 6, 6)
    assert result["notes"] == [cli_media.duration_note(4, 6), "the last frame is not pinned: Grok (local CLI) "
                               "image_to_video takes the first frame only"]
    assert Path(result["artifact"]).name == "generated.mp4"
    args = next(e for e in fake_log(env.log) if e.get("args"))["args"]
    assert args["duration"] == 6 and args["resolution_name"] == "480p"
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert_valid_contract(job, "media", "job_v2", skill="generate2dmedia")
    assert (job["options"]["duration"], job["durationRequested"], job["durationUsed"], job["lastFrameUsed"]) == (
        6, 4, 6, False)
    code, plan, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--duration", "8",
                          "--out-dir", "out/plan", "--dry-run")
    assert code == 0 and plan["status"] == "dry-run" and plan["lastFrameUsed"] is False, err
    assert (plan["durationRequested"], plan["durationUsed"], plan["notes"]) == (8, 10, [cli_media.duration_note(8, 10)])


def test_local_video_failure_names_the_provider_reason(env, capsys, monkeypatch):
    """A refused image_to_video call surfaces Grok's own reason in the error line, not a bare GENERATION_FAILED."""
    env.installed["grok"] = True
    monkeypatch.setenv("FAKE_GROK_DURATIONS", "5,8")  # a Grok that changed the lengths it renders
    code, result, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir",
                            "out/run")
    assert code == 1 and result is None
    assert err.startswith("error: local:grok-acp: GENERATION_FAILED: image_to_video reported a failure: "
                          "`duration` must be either 5 or 8 seconds. Got 6. (stop reason end_turn)"), err
    assert [s["status"] for s in ledger(env.project)] == ["failed"] and not (env.project / "out" / "run").exists()


def test_route_media_doc_matches_the_tool():
    """route-media.md: single-line commands whose options exist, the config file paths and the output contract."""
    import re

    text = (SKILLS_DIR / "generate2dmedia" / "references" / "route-media.md").read_text(encoding="utf-8")
    commands = [line for block in re.findall(r"```bash\n(.*?)```", text, re.S) for line in block.splitlines() if line]
    assert len(commands) >= 5
    parser = route_media.build_parser()
    verbs = next(a for a in parser._actions if isinstance(a, route_media.argparse._SubParsersAction)).choices
    for command in commands:
        assert command.startswith('python "<skill-dir>/scripts/route_media.py" '), command
        verb = command.split('route_media.py" ', 1)[1].split()[0]
        options = set(re.findall(r"\s(--[a-z-]+)", command))
        assert options <= set(verbs[verb]._option_string_actions), command
    for phrase in (r"%APPDATA%\agent-sprite-forge\config.json", "~/.config/agent-sprite-forge/config.json",
                   '{"status":"no-route","fallback":"codeart2d"}', route_media.FAKE_ENV, "estimateUsd"):
        assert phrase in text, phrase


# --------------------------------------------------------------------------- the test seam

def test_fake_seam_receives_the_checked_command(env, capsys, monkeypatch, tmp_path):
    fake = tmp_path / "fake_route_media.py"
    fake.write_text(
        "import json, sys\n"
        "print(json.dumps({'status': 'ok', 'route': 'api:fake', 'argv': sys.argv[1:]}))\n"
        "sys.exit(7 if '--route' in sys.argv and sys.argv[sys.argv.index('--route') + 1] == 'xai' else 0)\n",
        encoding="utf-8")
    monkeypatch.setenv(route_media.FAKE_ENV, str(fake))
    argv = ["image", "--prompt-file", "prompt.txt", "--reference", "first.png", "--out-dir", "out/x"]
    code, result, _ = run(capsys, *argv, transport=Refuse())
    assert code == 0 and result == {"status": "ok", "route": "api:fake", "argv": argv}
    assert run(capsys, *argv, "--route", "xai")[0] == 7  # the fake's exit code is returned unchanged
    code, result, err = run(capsys, "image", "--prompt-file", "missing.txt", "--out-dir", "out/x")
    assert code == 1 and result is None and "does not exist" in err  # checked before the fake
    code, result, _ = run(capsys, "resolve", "--kind", "video")
    assert code == 0 and result["argv"] == ["resolve", "--kind", "video"]
    assert not (env.project / "out").exists() and not (env.project / ".forge").exists()


def test_fake_seam_from_a_subprocess(env, tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text("import sys\nprint('{\"status\":\"no-route\",\"fallback\":\"codeart2d\"}')\nsys.exit(3)\n",
                    encoding="utf-8")
    result = run_cli([script_path("generate2dmedia", "route_media"), "video", "--prompt-file", "prompt.txt",
                      "--reference", "first.png", "--out-dir", "out/v"], cwd=env.project,
                     env={route_media.FAKE_ENV: str(fake)}, timeout=120)
    assert result.returncode == 3 and json.loads(result.stdout) == {"status": "no-route", "fallback": "codeart2d"}


def test_unexpected_errors_are_one_clean_line(env, capsys, monkeypatch):
    configure(OPENAI_API_KEY=OPENAI_KEY)

    def broken(*args, **kwargs):
        raise RuntimeError(f"state broke with {OPENAI_KEY}")
    monkeypatch.setattr(route_media, "generate", broken)
    code, out, err = run(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/a")
    assert code == 1 and out is None and err.startswith("error: internal error (RuntimeError: state broke with")
    assert OPENAI_KEY not in err and "Traceback" not in err


@pytest.mark.parametrize("extra, message", [
    (["image", "--size", "big"], "--size must be auto or WIDTHxHEIGHT"),
    (["video", "--reference", "first.png", "--duration", "20"], "--duration must be 1..15"),
    (["video", "--reference", "nope.png"], "reference image nope.png does not exist"),
    (["video", "--reference", "first.png", "--last-frame", "gone.png"], "last frame gone.png does not exist"),
])
def test_invalid_requests_fail_before_any_route(env, capsys, monkeypatch, extra, message):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    code, out, err = run(capsys, extra[0], "--prompt-file", "prompt.txt", *extra[1:], "--out-dir", "out/x",
                         transport=Refuse())
    assert code == 1 and out is None and message in err
    assert not (env.project / "out").exists() and not (env.project / ".forge").exists()


def test_new_options_fail_cleanly_before_any_route(env, capsys, monkeypatch):
    """--keyframe, --provider-option and --model problems are one error line (exit 1), checked before the test fake
    and before any route; nothing is sent or written."""
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    for extra, message in ((["--keyframe", "first.png"], "PATH@SECONDS"), (["--keyframe", "gone.png@2"], "keyframe gone.png"),
                           (["--provider-option", "=1"], "KEY=VALUE"), (["--model", "bad model!"], "--model must be")):
        code, out, err = run(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "first.png", *extra,
                             "--out-dir", "out/x", transport=Refuse())
        assert code == 1 and out is None and message in err and "internal error" not in err, err
    assert not (env.project / "out").exists() and not (env.project / ".forge").exists()
