"""Offline provider-contract tests. These do not claim live provider quality.

No test opens a network connection: transports are fakes, or the real
Transport with its network boundary (_open, socket creation) replaced.
"""
import base64
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from urllib import error

from PIL import Image
import pytest

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "skills/generate2dmedia/scripts/generate_media.py"
spec = importlib.util.spec_from_file_location("forge_media", SOURCE)
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)
ledger_mod = media.media_ledger

OPENAI_KEY = "sk-canary-openai-" + "0123456789abcdef" * 2
XAI_KEY = "xai-canary-xai-" + "fedcba9876543210" * 2
MP4 = b"\x00\x00\x00\x18ftypmp42data"


def png():
    output = io.BytesIO()
    Image.new("RGBA", (32, 48), (30, 100, 180, 128)).save(output, "PNG")
    return output.getvalue()


def b64_image():
    return {"data": [{"b64_json": base64.b64encode(png()).decode()}]}


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Never see real credentials or caps; never write a ledger into the repo."""
    for name in ("OPENAI_API_KEY", "XAI_API_KEY", ledger_mod.MAX_PAID_ENV):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)


@pytest.fixture
def inputs(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Preserve this hero's silhouette. Idle in place.", encoding="utf-8")
    ref = tmp_path / "base.png"
    ref.write_bytes(png())
    return prompt, ref, tmp_path / "output"


def project(inputs):
    return inputs[0].parent / "project"


def argv(inputs, mode="image", provider="openai", extra=(), out=None):
    prompt, ref, default_out = inputs
    model = "gpt-image-2.5-sunburst" if provider == "openai" else (
        "grok-imagine-video-1.5" if mode == "video" else "grok-imagine-image-2.0")
    return [mode, "--provider", provider, "--model", model, "--prompt-file", str(prompt),
            "--out-dir", str(out or default_out), "--project-dir", str(project(inputs)), *extra]


def args(inputs, mode="image", provider="openai", extra=(), out=None):
    return media.parser().parse_args(argv(inputs, mode, provider, extra, out))


def ledger_states(inputs):
    return list(ledger_mod.Ledger(project(inputs)).entries().values())


class Fake:
    def __init__(self, results=(), download=MP4, request_id=None):
        self.results = iter(results)
        self.calls = []
        self.downloads = []
        self.content = download
        self.request_id = request_id

    def api(self, method, url, key, body=None, content_type=None, timeout=90, meta=None):
        self.calls.append((method, url, body, content_type, timeout))
        if meta is not None and self.request_id:
            meta["providerRequestId"] = self.request_id
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def download(self, url, timeout=90):
        self.downloads.append(url)
        return self.content


class Refuse:
    """A transport that must never be used."""

    def api(self, *a, **k):
        raise AssertionError("network used")

    download = api


def test_dry_run_never_uses_credentials_or_network(inputs):
    transport = Fake()
    result = media.execute(args(inputs), transport)
    assert result["execution"] == "dry-run"
    assert result["paidRequests"] == 1
    assert not inputs[2].exists() and not transport.calls
    assert not (project(inputs) / ".forge").exists()  # dry-run writes no ledger
    assert "base64" not in json.dumps(result)


def test_openai_edit_multipart_native_alpha(inputs):
    a = args(inputs, extra=["--reference", str(inputs[1]), "--transparent"])
    plan, body, mime, _ = media.prepare(a)
    assert plan["endpoint"] == "https://api.openai.com/v1/images/edits"
    assert mime.startswith("multipart/form-data; boundary=")
    assert b'name="image[]"' in body and png() in body
    assert b'name="background"\r\n\r\ntransparent' in body
    assert b"response_format" not in body


def test_xai_edit_is_json_not_openai_multipart(inputs):
    a = args(inputs, provider="xai", extra=["--reference", str(inputs[1]), "--resolution", "2k"])
    plan, body, mime, _ = media.prepare(a)
    fields = json.loads(body)
    assert mime == "application/json" and plan["endpoint"].endswith("/images/edits")
    assert fields["image"]["url"].startswith("data:image/png;base64,")
    assert fields["resolution"] == "2k" and fields["response_format"] == "b64_json"
    assert plan["references"][0]["size"] == [32, 48]


def test_video_preserves_aspect_and_pins_frames(inputs):
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--last-frame", str(inputs[1])])
    _, body, _, _ = media.prepare(a)
    fields = json.loads(body)
    assert fields["image"] == fields["last_frame"]
    assert fields["generate_audio"] is False
    assert "aspect_ratio" not in fields


@pytest.mark.parametrize("extra", [
    ["--duration", "0"], ["--duration", "16"], ["--timeout", "nan"],
    ["--resolution", "1080p", "--last-frame", "REF"],
    ["--model", "grok-imagine-video", "--last-frame", "REF"],
    ["--model", "invented-video-model"],
])
def test_invalid_video_plan_fails_before_request(inputs, extra):
    extra = [str(inputs[1]) if v == "REF" else v for v in extra]
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), *extra])
    transport = Fake()
    with pytest.raises(media.MediaError):
        media.execute(a, transport)
    assert not transport.calls and not inputs[2].exists()


def test_existing_directory_blocks_paid_submission(inputs, monkeypatch):
    inputs[2].mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    transport = Fake()
    with pytest.raises(media.MediaError, match="already exists"):
        media.execute(args(inputs, extra=["--execute"]), transport)
    assert not transport.calls
    assert not ledger_states(inputs)  # refused before any reservation


def test_image_saved_with_actual_format_and_hash(inputs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    transport = Fake([b64_image()])
    result = media.execute(args(inputs, extra=["--execute"]), transport)
    assert result["status"] == "done"
    assert (inputs[2] / "generated.png").read_bytes() == png()
    assert result["artifact"]["hasAlphaChannel"] is True
    assert result["returnedModel"] is None  # do not fabricate server model metadata
    assert OPENAI_KEY not in (inputs[2] / "job.json").read_text(encoding="utf-8")
    assert len(transport.calls) == 1
    assert [s["status"] for s in ledger_states(inputs)] == ["done"]


def test_unknown_post_outcome_never_retries(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    transport = Fake([media.MediaError("network failure")])
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute"])
    with pytest.raises(media.MediaError):
        media.execute(a, transport)
    job = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "submit_unknown" and len(transport.calls) == 1
    r = media.parser().parse_args(["resume", "--job", str(inputs[2] / "job.json")])
    with pytest.raises(media.MediaError, match="request ID"):
        media.resume(r, transport)
    assert len(transport.calls) == 1
    assert [s["status"] for s in ledger_states(inputs)] == ["unknown"]  # reservation kept


def test_video_resume_only_polls_and_downloads(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    first = Fake([{"request_id": "request-123"}, media.MediaError("poll disconnected")])
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute"])
    with pytest.raises(media.MediaError):
        media.execute(a, first)
    assert [c[0] for c in first.calls] == ["POST", "GET"]
    assert [s["status"] for s in ledger_states(inputs)] == ["reserved"]  # still in flight
    transport = Fake([{"status": "done", "model": "grok-imagine-video-1.5", "video": {
        "url": "https://media.example/clip.mp4?token=PRIVATE", "duration": 4}}])
    path = inputs[2] / "job.json"
    r = media.parser().parse_args(["resume", "--job", str(path)])
    result = media.resume(r, transport)
    assert result["status"] == "done" and [c[0] for c in transport.calls] == ["GET"]
    assert "PRIVATE" not in path.read_text(encoding="utf-8")
    assert [s["status"] for s in ledger_states(inputs)] == ["done"]
    assert result["receipt"]["outcomeCode"] == "ok" and result["receipt"]["providerMs"] is not None
    assert media.resume(r, Fake())["status"] == "done"  # done skips network
    (inputs[2] / "generated.mp4").write_bytes(b"changed")
    with pytest.raises(media.MediaError, match="changed"):
        media.resume(r, Fake())


@pytest.mark.parametrize("status", ["failed", "expired", "mystery"])
def test_poll_terminal_or_unknown_never_resubmits(inputs, status):
    inputs[2].mkdir()
    job = {"requestId": "req-1", "status": "pending"}
    transport = Fake([{"status": status}])
    with pytest.raises(media.MediaError):
        media.poll_video(inputs[2] / "job.json", job, "key", transport, 1, 1)
    assert [c[0] for c in transport.calls] == ["GET"]


def test_poll_timeout_preserves_request_id(inputs):
    inputs[2].mkdir()
    ticks = iter([0, 0, 0, .5, 2])
    job = {"requestId": "req-1", "status": "pending"}
    transport = Fake([{"status": "pending"}])
    with pytest.raises(media.MediaError, match="timeout"):
        media.poll_video(inputs[2] / "job.json", job, "key", transport, 1, 1,
                         clock=lambda: next(ticks), sleep=lambda _: None)
    assert job["status"] == "pending_timeout" and job["requestId"] == "req-1"


def test_bad_mp4_and_bad_image_rejected(inputs):
    """Nothing invalid is published; returned image bytes are kept, not deleted (F-04)."""
    inputs[2].mkdir()
    with pytest.raises(media.MediaError, match="MP4"):
        media.write_artifact(inputs[2], b"<html>error</html>", "video")
    assert not list(inputs[2].iterdir())  # resume can download a video again
    with pytest.raises(media.PartialArtifact, match="image") as caught:
        media.write_artifact(inputs[2], b"bad png", "image")
    assert not list(inputs[2].glob("generated*"))
    assert (inputs[2] / caught.value.partial["path"]).read_bytes() == b"bad png"


def test_api_redirect_does_not_forward_key():
    with pytest.raises(media.MediaError, match="redirect"):
        media.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


def test_resume_recovers_artifact_published_before_job_commit(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    inputs[2].mkdir()
    (inputs[2] / "generated.mp4").write_bytes(MP4)
    path = inputs[2] / "job.json"
    media.save_json(path, {"schemaVersion": 1, "provider": "xai", "kind": "video",
                          "status": "downloading", "requestId": "req-recover"})
    transport = Fake([{"status": "done", "video": {"url": "https://media.example/clip.mp4"}}])
    result = media.resume(media.parser().parse_args(["resume", "--job", str(path)]), transport)
    assert result["status"] == "done"
    assert [call[0] for call in transport.calls] == ["GET"]
    assert (inputs[2] / "generated.mp4").read_bytes() == MP4


def test_recovery_preserves_conflicting_existing_artifact(inputs):
    inputs[2].mkdir()
    original = b"\x00\x00\x00\x18ftypmp42original"
    (inputs[2] / "generated.mp4").write_bytes(original)
    with pytest.raises(media.MediaError, match="already exists"):
        media.write_artifact(inputs[2], b"\x00\x00\x00\x18ftypmp42different", "video", adopt_identical=True)
    assert (inputs[2] / "generated.mp4").read_bytes() == original


# --- A4-T1: artifact safety -------------------------------------------------

def no_hardlinks(src, dst):
    # Windows reports FAT/exFAT hard links as WinError 1; POSIX exFAT/vfat give EPERM.
    raise OSError(errno.EPERM, "Operation not permitted (no hard links on this volume)")


def test_hardlink_unsupported_keeps_paid_image(inputs, monkeypatch):
    """F-04, repro repo-audit/probe_media_link_failure.py: the paid image used to be deleted."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setattr(media.os, "link", no_hardlinks)
    transport = Fake([b64_image()])
    result = media.execute(args(inputs, extra=["--execute"]), transport)
    assert result["status"] == "done" and len(transport.calls) == 1
    assert (inputs[2] / "generated.png").read_bytes() == png()
    assert not list(inputs[2].glob(".artifact-*"))  # temp removed only after sha256 verification
    assert "partialArtifact" not in result
    assert [s["status"] for s in ledger_states(inputs)] == ["done"]


@pytest.mark.parametrize("failure", ["disk_full", "corrupt_copy"])
def test_unpublishable_image_is_kept_as_partial_artifact(inputs, monkeypatch, failure):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setattr(media.os, "link", no_hardlinks)
    write_new = media._write_new

    def flaky(path, content):
        if not path.name.startswith("generated"):
            return write_new(path, content)
        if failure == "disk_full":
            raise OSError(errno.ENOSPC, "No space left on device")
        return write_new(path, content[:-1])

    monkeypatch.setattr(media, "_write_new", flaky)
    with pytest.raises(media.PartialArtifact):
        media.execute(args(inputs, extra=["--execute"]), Fake([b64_image()]))
    job = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    kept = inputs[2] / job["partialArtifact"]["path"]
    assert kept.read_bytes() == png() and job["partialArtifact"]["sha256"] == media.digest(png())
    assert not (inputs[2] / "generated.png").exists()  # never a truncated final file
    assert job["status"] == "interrupted" and job["receipt"]["outcomeCode"] == "partial_artifact"
    assert [s["status"] for s in ledger_states(inputs)] == ["done"]  # the provider delivered it


# --- A4-T2: console encoding ------------------------------------------------

def cp1252_console(monkeypatch):
    streams = {}
    for name in ("stdout", "stderr"):
        raw = io.BytesIO()
        streams[name] = raw
        monkeypatch.setattr(sys, name, io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    return streams


def test_success_under_cp1252_exits_0(inputs, monkeypatch):
    """F-14: a paid success used to end in UnicodeEncodeError and exit 1."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    inputs[0].write_text("Héroe 勇者: idle loop", encoding="utf-8")
    out = inputs[2].parent / "輸出-hero"
    streams = cp1252_console(monkeypatch)
    code = media.main(argv(inputs, extra=["--execute"], out=out), transport=Fake([b64_image()]))
    sys.stdout.flush()
    sys.stderr.flush()
    printed = streams["stdout"].getvalue().decode("ascii")  # console output is ASCII
    assert code == 0 and streams["stderr"].getvalue() == b""
    assert len(printed.splitlines()) == 1 and json.loads(printed)["status"] == "done"
    assert (out / "generated.png").read_bytes() == png()
    assert (sys.stdout.encoding, sys.stdout.errors) == ("utf-8", "backslashreplace")
    # argparse echoes a bad value; on the original strict cp1252 stream that would crash.
    cp1252_console(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        media.main(["image", "--provider", "勇者"])
    assert stopped.value.code == 2


@pytest.mark.parametrize("command", [[], ["image"], ["video"], ["resume"], ["batch"]])
def test_help_works_under_cp1252(command):
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, str(SOURCE), *command, "--help"], capture_output=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode("ascii").startswith("usage:")


# --- A4-T3: error fidelity --------------------------------------------------

class HTTPFailure(media.Transport):
    def __init__(self, status, payload, headers=None):
        self.status, self.payload, self.headers = status, payload, headers or {}

    def _open(self, req, timeout, limit):
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        raise error.HTTPError(req.full_url, self.status, "error", self.headers, io.BytesIO(body))


def test_http_error_fields_whitelisted(inputs, monkeypatch):
    """F-05 / issue #11: the provider's reason is kept, but only whitelisted, scrubbed fields."""
    payload = {"error": {
        "message": f"Upload URL is required for zero data retention teams; key {OPENAI_KEY} "
                   "see https://signed.example/upload?sig=SIGNATURE-PRIVATE " + "detail " * 80,
        "type": "invalid_request_error", "param": "upload_url", "code": "upload_url_required",
        "internal_trace": "TRACE-PRIVATE", "prompt": "PROMPT-PRIVATE"}, "debug": "DEBUG-PRIVATE"}
    meta = {}
    with pytest.raises(media.MediaError) as caught:
        HTTPFailure(400, payload, {"x-request-id": "req_abc123"}).api(
            "POST", "https://api.openai.com/v1/images/generations", OPENAI_KEY, b"{}", meta=meta)
    exc = caught.value
    assert exc.sent is True and exc.code == "invalid_request" and meta["providerRequestId"] == "req_abc123"
    assert set(exc.provider) == {"httpStatus", "code", "type", "param", "message", "requestId"}
    assert exc.provider["param"] == "upload_url" and exc.provider["code"] == "upload_url_required"
    assert exc.provider["message"].startswith("Upload URL is required") and len(exc.provider["message"]) <= 300
    shown = json.dumps(exc.provider) + str(exc)
    for private in (OPENAI_KEY, "SIGNATURE-PRIVATE", "signed.example", "TRACE-PRIVATE", "PROMPT-PRIVATE", "DEBUG-PRIVATE"):
        assert private not in shown
    # End to end: the job records the fields, the request is not retried, the ledger releases it.
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    with pytest.raises(media.MediaError, match="upload_url"):
        media.execute(args(inputs, extra=["--execute"]), HTTPFailure(400, payload))
    job = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["error"]["param"] == "upload_url"
    assert job["receipt"]["outcomeCode"] == "invalid_request"
    assert [s["status"] for s in ledger_states(inputs)] == ["failed"]


@pytest.mark.parametrize("status,payload,expected", [
    (401, {"error": {"message": "Incorrect API key provided: sk-abc***wxyz", "code": "invalid_api_key"}}, "auth"),
    (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "quota"),
    (429, {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}, "rate_limit"),
    (400, {"error": {"message": "Request was rejected by the safety system", "code": "moderation_blocked"}}, "moderation"),
    (403, {"code": "The caller does not have permission", "error": "Team is not entitled to this model"}, "entitlement"),
    (404, {"error": {"message": "The model does not exist", "code": "model_not_found"}}, "entitlement"),
    (500, b"<html>upstream error</html>", "provider_error"),
])
def test_http_errors_are_classified(status, payload, expected):
    with pytest.raises(media.MediaError) as caught:
        HTTPFailure(status, payload).api("POST", "https://api.x.ai/v1/images/generations", XAI_KEY, b"{}")
    assert caught.value.code == expected
    assert "sk-abc" not in str(caught.value)  # masked key fragments are scrubbed too


def test_xai_error_shape_keeps_message():
    with pytest.raises(media.MediaError) as caught:
        HTTPFailure(400, {"code": "Client specified an invalid argument", "error": "upload_url is required"}).api(
            "POST", "https://api.x.ai/v1/videos/generations", XAI_KEY, b"{}")
    assert caught.value.provider["message"] == "upload_url is required"


class NetworkFailure(media.Transport):
    def __init__(self, exc):
        self.exc = exc

    def _open(self, req, timeout, limit):
        raise self.exc


@pytest.mark.parametrize("exc", [error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
                                 error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
                                 media.NotSent("Connecting to api.openai.com timed out")])
def test_dns_failure_is_not_sent(inputs, monkeypatch, capsys, exc):
    """F-05: a request that never left the machine is not_sent, not submit_unknown."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    code = media.main(argv(inputs, extra=["--execute"]), transport=NetworkFailure(exc))
    assert code == 1 and "not sent" in capsys.readouterr().err
    job = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "not_sent" and job["receipt"]["outcomeCode"] == "not_sent"
    assert [s["status"] for s in ledger_states(inputs)] == ["not_sent"]
    assert ledger_mod.Ledger(project(inputs)).totals()["calls"] == 0


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError(errno.ECONNRESET, "reset"),
                                 error.URLError(ConnectionResetError(errno.ECONNRESET, "reset while sending"))])
def test_failure_after_sending_is_submit_unknown(inputs, monkeypatch, exc):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    with pytest.raises(media.MediaError) as caught:
        media.execute(args(inputs, extra=["--execute"]), NetworkFailure(exc))
    assert caught.value.code == "submit_unknown" and caught.value.sent is None
    job = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "submit_unknown"
    assert [s["status"] for s in ledger_states(inputs)] == ["unknown"]


class _Socket:
    """Connected-socket stand-in: sending fails, or the response never arrives."""

    def __init__(self, fail_send):
        self.fail_send, self.sent, self.reads = fail_send, b"", 0

    def sendall(self, data):
        if self.fail_send:
            raise ConnectionResetError(errno.ECONNRESET, "reset while sending")
        self.sent += bytes(data)

    def makefile(self, mode):
        owner = self

        class Reader(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                owner.reads += 1
                raise TimeoutError("timed out")
        return io.BufferedReader(Reader())

    def close(self):
        pass


def test_real_transport_tells_connect_failures_from_later_ones(monkeypatch):
    """The HTTPS handler tracks the connect phase, so only pre-connection failures are not_sent."""
    def refuse(*a, **k):
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(socket, "create_connection", refuse)
    with pytest.raises(media.MediaError) as caught:
        media.Transport().api("POST", "https://api.openai.com/v1/images/generations", OPENAI_KEY, b"{}")
    assert (caught.value.code, caught.value.sent) == ("not_sent", False)
    for fail_send in (True, False):
        sockets = []

        def connected(self, fail_send=fail_send):
            self.sock = _Socket(fail_send)
            sockets.append(self.sock)

        monkeypatch.setattr(media.http.client.HTTPSConnection, "connect", connected)
        with pytest.raises(media.MediaError) as caught:
            media.Transport().api("POST", "https://api.openai.com/v1/images/generations", OPENAI_KEY, b"{}")
        assert (caught.value.code, caught.value.sent) == ("submit_unknown", None)
        # The send failed mid-request, or the full request went out and the read timed out.
        assert (sockets[0].sent == b"", sockets[0].reads > 0) == ((True, False) if fail_send else (False, True))


# --- A4-T4: flags -----------------------------------------------------------

def test_submit_timeout_bounds(inputs, monkeypatch):
    """F-16: image POSTs wait 300 s by default (was 180), at most 600, and keep request ids."""
    for value in ("0", "601", "nan"):
        with pytest.raises(media.MediaError, match="submit-timeout"):
            media.prepare(args(inputs, extra=["--submit-timeout", value]))
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    transport = Fake([b64_image()], request_id="req_provider_42")
    job = media.execute(args(inputs, extra=["--execute"]), transport)
    assert transport.calls[0][4] == 300
    assert job["providerRequestId"] == "req_provider_42" and len(job["clientRequestId"]) == 32
    transport = Fake([b64_image()])
    extra = ["--execute", "--allow-duplicate", "--submit-timeout", "600", "--timeout", "450"]
    media.execute(args(inputs, extra=extra, out=inputs[2].parent / "second"), transport)
    assert transport.calls[0][4] == 450  # --timeout still caps the submit wait


def test_custom_base_url_requires_opt_in(inputs, monkeypatch):
    """PR #16: a custom base URL receives the key, so it is https-only and opt-in."""
    custom = ["--base-url", "https://gateway.example/v1/"]
    with pytest.raises(media.MediaError, match="allow-custom-base-url"):
        media.prepare(args(inputs, extra=custom))
    for bad in ("http://gateway.example/v1", "https://user:pw@gateway.example/v1", "https://gateway.example/v1?x=1"):
        with pytest.raises(media.MediaError, match="https"):
            media.prepare(args(inputs, extra=["--base-url", bad, "--allow-custom-base-url"]))
    plan = media.prepare(args(inputs, extra=[*custom, "--allow-custom-base-url"]))[0]
    assert plan["endpoint"] == "https://gateway.example/v1/images/generations"
    assert plan["consent"]["apiHost"] == "gateway.example"
    # resume refuses to send the key to a job's custom host without the same opt-in
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute", *custom, "--allow-custom-base-url"])
    with pytest.raises(media.MediaError):
        media.execute(a, Fake([{"request_id": "req-gw"}, media.MediaError("poll disconnected")]))
    job_path = str(inputs[2] / "job.json")
    with pytest.raises(media.MediaError, match="allow-custom-base-url"):
        media.resume(media.parser().parse_args(["resume", "--job", job_path]), Refuse())
    transport = Fake([media.MediaError("poll disconnected", code="network")])
    with pytest.raises(media.MediaError, match="poll disconnected"):
        media.resume(media.parser().parse_args(["resume", "--job", job_path, "--allow-custom-base-url"]), transport)
    assert transport.calls[0][1] == "https://gateway.example/v1/videos/req-gw"


def test_upload_url_is_sent_but_never_stored(inputs, monkeypatch):
    """xAI ZDR (issue #11): upload_url goes in the body; the signed URL is not logged."""
    signed = "https://bucket.example/out.mp4?X-Signature=SIGNED-PRIVATE"
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    with pytest.raises(media.MediaError, match="https"):
        media.prepare(args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--upload-url", "ftp://x.example/a"]))
    transport = Fake([{"request_id": "req-zdr"}, {"status": "done", "video": {"url": "https://media.example/v.mp4"}}])
    job = media.execute(args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute", "--upload-url", signed]), transport)
    assert json.loads(transport.calls[0][2])["upload_url"] == signed
    assert job["uploadUrl"]["host"] == "bucket.example"
    assert "SIGNED-PRIVATE" not in (inputs[2] / "job.json").read_text(encoding="utf-8")
    assert "upload_url" not in job["options"]


# --- A4-T6: duplicate guard, receipts, job v2 --------------------------------

def test_duplicate_successful_request_refused(inputs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    transport = Fake([b64_image(), b64_image()])
    media.execute(args(inputs, extra=["--execute"]), transport)
    second = inputs[2].parent / "again"
    with pytest.raises(media.MediaError, match="already succeeded") as caught:
        media.execute(args(inputs, extra=["--execute"], out=second), transport)
    assert caught.value.code == "duplicate" and len(transport.calls) == 1 and not second.exists()
    dry = media.execute(args(inputs, out=second), Refuse())
    assert any("already succeeded" in w for w in dry["warnings"])
    job = media.execute(args(inputs, extra=["--execute", "--allow-duplicate"], out=second), transport)
    assert job["receipt"]["attempt"] == 2 and len(transport.calls) == 2


def test_unsettled_identical_request_also_needs_allow_duplicate(inputs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    with pytest.raises(media.MediaError):
        media.execute(args(inputs, extra=["--execute"]), NetworkFailure(TimeoutError("timed out")))
    with pytest.raises(media.MediaError, match="unknown outcome"):
        media.execute(args(inputs, extra=["--execute"], out=inputs[2].parent / "b"), Refuse())


def test_v1_job_resume_still_works(inputs, monkeypatch):
    """Jobs written by the previous adapter (schemaVersion 1) resume unchanged and stay v1."""
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    inputs[2].mkdir()
    path = inputs[2] / "job.json"
    v1 = {"schemaVersion": 1, "provider": "xai", "kind": "video",
          "endpoint": "https://api.x.ai/v1/videos/generations", "requestedModel": "grok-imagine-video-1.5",
          "options": {"model": "grok-imagine-video-1.5", "duration": 4, "resolution": "720p"},
          "promptSha256": "0" * 64, "references": [], "lastFrame": None, "keyEnv": "XAI_API_KEY",
          "paidRequests": 1, "automaticPostRetries": 0, "outDir": str(inputs[2]), "execution": "execute",
          "status": "pending_timeout", "requestId": "req-v1"}
    media.save_json(path, v1)
    transport = Fake([{"status": "done", "video": {"url": "https://media.example/v1.mp4", "duration": 4}}])
    assert media.main(["resume", "--job", str(path)], transport=transport) == 0
    job = json.loads(path.read_text(encoding="utf-8"))
    assert job["schemaVersion"] == 1 and job["status"] == "done" and "receipt" not in job
    assert (inputs[2] / "generated.mp4").read_bytes() == MP4
    assert [c[1] for c in transport.calls] == ["https://api.x.ai/v1/videos/req-v1"]
    assert not list(Path.cwd().rglob("ledger.jsonl"))


def _vendored_validator(definition):
    """Validator for a $def of A0's vendored media schema, or None until A0 is merged."""
    folder = ROOT / "skills/generate2dmedia/references/schemas"
    if not (folder / "media.schema.json").is_file():
        return None
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    resources = []
    for path in folder.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        resource = referencing.Resource.from_contents(document, default_specification=referencing.jsonschema.DRAFT202012)
        resources += [(path.name, resource)] + ([(document["$id"], resource)] if "$id" in document else [])
    media_schema = json.loads((folder / "media.schema.json").read_text(encoding="utf-8"))
    assert definition in media_schema.get("$defs", {}), f"media.schema.json lacks $defs/{definition} (Appendix B)"
    base = media_schema.get("$id", "media.schema.json")
    return jsonschema.Draft202012Validator({"$ref": f"{base}#/$defs/{definition}"},
                                           registry=referencing.Registry().with_resources(resources))


def test_job_validates_as_job_v2(inputs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    job = media.execute(args(inputs, extra=["--execute", "--purpose", "hero idle v2"]), Fake([b64_image()]))
    saved = json.loads((inputs[2] / "job.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(job))
    # Appendix B job_v2 fields
    assert saved["schemaVersion"] == 2 and media.media_ledger.SHA256.fullmatch(saved["fingerprint"])
    assert set(saved["receipt"]) >= {"startedAt", "submittedAt", "completedAt", "wallMs", "providerMs",
                                     "attempt", "purpose", "toolVersion", "outcomeCode"}
    receipt = saved["receipt"]
    assert receipt["purpose"] == "hero idle v2" and receipt["attempt"] == 1 and receipt["outcomeCode"] == "ok"
    assert receipt["toolVersion"] == media.TOOL and receipt["wallMs"] >= 0 and receipt["providerMs"] >= 0
    assert set(saved["estimate"]) >= {"usd", "basis", "pricesVersion"}
    assert set(saved["artifact"]) >= {"path", "sha256", "bytes"}
    validator = _vendored_validator("job_v2")
    if validator is not None:
        validator.validate(saved)


def test_media_documents_validate_against_vendored_schema(inputs, monkeypatch):
    """job_v2, ledger_line_v1 and prices_v1 against A0's frozen schema once it is merged."""
    if _vendored_validator("job_v2") is None:
        pytest.skip("A0-contracts media.schema.json is not merged into this branch yet")
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    failed = inputs[2].parent / "failed"
    media.execute(args(inputs, extra=["--execute"]), Fake([b64_image()]))
    with pytest.raises(media.MediaError):
        media.execute(args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute"], out=failed),
                      Fake([{"request_id": "req-schema"}, media.MediaError("poll disconnected", code="network")]))
    for path in (inputs[2] / "job.json", failed / "job.json"):  # a done image and a pending video
        _vendored_validator("job_v2").validate(json.loads(path.read_text(encoding="utf-8")))
    for line in ledger_mod.Ledger(project(inputs)).lines():
        _vendored_validator("ledger_line_v1").validate(line)
    _vendored_validator("prices_v1").validate(ledger_mod.load_prices())


def test_price_table_schema_id_is_namespaced(tmp_path):
    """prices.json says generate2dmedia.prices.v1 (A0's prices_v1); copies still saying prices_v1 load the same."""
    shipped = json.loads(ledger_mod.PRICES_PATH.read_text(encoding="utf-8"))
    assert shipped["schema"] == "generate2dmedia.prices.v1"
    assert ledger_mod.load_prices() == shipped
    legacy = tmp_path / "prices-legacy.json"
    legacy.write_text(json.dumps({**shipped, "schema": "prices_v1"}), encoding="utf-8")
    assert ledger_mod.load_prices(legacy) == {**shipped, "schema": "prices_v1"}
    assert ledger_mod.prices_version(ledger_mod.load_prices(legacy)) == ledger_mod.prices_version(shipped)


# --- A4-T7: batch -------------------------------------------------------------

def write_jobs(tmp_path, count, provider="openai"):
    jobs = []
    for i in range(count):
        (tmp_path / f"p{i}.txt").write_text(f"Batch prop number {i}", encoding="utf-8")
        job = {"id": f"j{i}", "command": "image", "provider": provider, "prompt_file": f"p{i}.txt", "out_dir": f"out/j{i}",
               "model": "gpt-image-2.5-sunburst" if provider == "openai" else "grok-imagine-image-2.0"}
        if provider == "xai":
            job.update(resolution="1k", quality="low", reference=["base.png"])
        jobs.append(job)
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def quota_error():
    return media.MediaError("API HTTP 429 insufficient_quota: You exceeded your current quota; request was not retried",
                            code="quota", sent=True)


def test_batch_dry_run_sends_nothing(inputs, tmp_path, capsys):
    jobs = write_jobs(tmp_path, 3, provider="xai")
    code = media.main(["batch", str(jobs), "--project-dir", str(project(inputs))], transport=Refuse())
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["execution"] == "dry-run" and summary["calls"] == 3
    assert summary["estimateUsd"] == pytest.approx(3 * (0.04 + 0.01)) and summary["unpricedCalls"] == 0
    assert [row["action"] for row in summary["consent"]] == ["send"] * 3
    assert {row["model"] for row in summary["consent"]} == {"grok-imagine-image-2.0"}
    assert not (tmp_path / "out").exists() and not (project(inputs) / ".forge").exists()
    assert not (tmp_path / "jobs.progress.json").exists()


def test_batch_stops_on_quota_and_never_retries(inputs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 4)
    base = ["batch", str(jobs), "--project-dir", str(project(inputs)), "--execute"]
    transport = Fake([b64_image(), quota_error()])
    assert media.main(base, transport=transport) == 1
    assert "stopped at j1: quota" in capsys.readouterr().err
    assert len(transport.calls) == 2  # j2 and j3 were never dispatched
    progress = json.loads((tmp_path / "jobs.progress.json").read_text(encoding="utf-8"))
    assert progress["schema"] == "generate2dmedia.batch_progress.v1" == media.BATCH_PROGRESS_SCHEMA
    assert progress["stopped"] and progress["remaining"] == ["j2", "j3"] and not progress["complete"]
    assert [(r["id"], r["outcomeCode"]) for r in progress["results"]] == [("j0", "ok"), ("j1", "quota")]
    # A second run reuses j0, leaves the failed j1 for a human and only sends j2 and j3.
    transport = Fake([b64_image(), b64_image()])
    assert media.main(base, transport=transport) == 1
    sent = [call[2] for call in transport.calls]
    assert len(sent) == 2 and all(b"number 1" not in body for body in sent)
    statuses = {r["id"]: r["status"] for r in json.loads((tmp_path / "jobs.progress.json").read_text(encoding="utf-8"))["results"]}
    assert statuses == {"j0": "reused", "j1": "left-for-human", "j2": "generated", "j3": "generated"}


def test_batch_rerun_over_a_legacy_progress_file(inputs, tmp_path, monkeypatch, capsys):
    """A progress file written before the id was namespaced (schema batch_progress_v1) does not
    disturb a re-run: the batch resumes from the job folders and rewrites the file with the new id."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 2)
    base = ["batch", str(jobs), "--project-dir", str(project(inputs)), "--execute"]
    assert media.main(base, transport=Fake([b64_image(), b64_image()])) == 0
    progress_path = tmp_path / "jobs.progress.json"
    legacy = json.loads(progress_path.read_text(encoding="utf-8"))
    progress_path.write_text(json.dumps({**legacy, "schema": "batch_progress_v1"}, indent=2), encoding="utf-8")
    capsys.readouterr()
    assert media.main(base, transport=Refuse()) == 0  # both jobs are reused; nothing is sent
    summary = json.loads(capsys.readouterr().out)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert summary["schema"] == progress["schema"] == "generate2dmedia.batch_progress.v1"
    assert [(r["id"], r["status"]) for r in progress["results"]] == [("j0", "reused"), ("j1", "reused")]
    assert progress["complete"] and not progress["stopped"]


def test_batch_progress_validates_against_vendored_schema(inputs, tmp_path, monkeypatch):
    """A stopped and a complete progress file against A0's batch_progress_v1 once it is vendored here."""
    vendored = ROOT / "skills/generate2dmedia/references/schemas/media.schema.json"
    defs = json.loads(vendored.read_text(encoding="utf-8")).get("$defs", {}) if vendored.is_file() else {}
    if "batch_progress_v1" not in defs:
        pytest.skip("A0-contracts media.schema.json with $defs/batch_progress_v1 is not merged into this branch yet")
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 3)
    base = ["batch", str(jobs), "--project-dir", str(project(inputs)), "--execute"]
    validator = _vendored_validator("batch_progress_v1")
    assert media.main(base, transport=Fake([b64_image(), quota_error()])) == 1  # stopped at j1
    validator.validate(json.loads((tmp_path / "jobs.progress.json").read_text(encoding="utf-8")))
    assert media.main(base, transport=Fake([b64_image()])) == 1  # complete; j1 is left for a human
    progress = json.loads((tmp_path / "jobs.progress.json").read_text(encoding="utf-8"))
    assert progress["complete"]
    validator.validate(progress)


def test_batch_progress_paths_are_relative(inputs, tmp_path, monkeypatch, capsys):
    """D25: the progress file records jobsFile and every jobDir relative to its own folder, never the absolute
    paths the batch resolved; it still validates as batch_progress_v1."""
    from forge_testutils import assert_valid_contract

    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "p0.txt").write_text("Batch prop number 0", encoding="utf-8")
    (jobs_dir / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "j0", "command": "image", "provider": "openai", "model": "gpt-image-2.5-sunburst",
         "prompt_file": "p0.txt", "out_dir": "../out/j0"}]}), encoding="utf-8")
    base = ["batch", str(jobs_dir / "jobs.json"), "--project-dir", str(project(inputs)), "--execute"]
    assert media.main(base, transport=Fake([b64_image()])) == 0
    text = (jobs_dir / "jobs.progress.json").read_text(encoding="utf-8")
    progress = json.loads(text)
    assert_valid_contract(progress, "media", "batch_progress_v1", skill="generate2dmedia")
    assert progress["jobsFile"] == "jobs.json" and progress["results"][0]["jobDir"] == "../out/j0"
    assert str(tmp_path) not in text and tmp_path.as_posix() not in text
    capsys.readouterr()
    # Another progress location keeps the paths relative to itself; the finished job is reused.
    assert media.main([*base, "--progress", str(tmp_path / "logs" / "run.json")], transport=Refuse()) == 0
    other = json.loads((tmp_path / "logs" / "run.json").read_text(encoding="utf-8"))
    assert other["jobsFile"] == "../jobs/jobs.json"
    assert other["results"] == [{"id": "j0", "status": "reused", "outcomeCode": "ok", "jobDir": "../out/j0"}]


def test_unexpected_errors_are_one_scrubbed_line(inputs, monkeypatch, capsys):
    """D27: an unexpected exception is one line, error: internal error (<Type>: <message>), with secrets
    removed; D29: receipts name the package release."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    code = media.main(argv(inputs, extra=["--execute"]),
                      transport=Fake([RuntimeError(f"transport crashed with {OPENAI_KEY}")]))
    err = capsys.readouterr().err
    assert code == 1 and "Traceback" not in err
    assert err.strip() == "error: internal error (RuntimeError: transport crashed with [redacted]); nothing was retried"
    assert media.TOOL == "generate_media/0.4.0" == "generate_media/" + ledger_mod.FORGE_PACKAGE_VERSION


class ThreadSafeFake(Fake):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()

    def api(self, method, url, key, body=None, content_type=None, timeout=90, meta=None):
        with self.lock:
            self.calls.append((method, url, body, content_type, timeout))
        return b64_image()


def test_batch_two_workers_send_each_job_once(inputs, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 5)
    transport = ThreadSafeFake()
    code = media.main(["batch", str(jobs), "--project-dir", str(project(inputs)), "--execute", "--workers", "2"],
                      transport=transport)
    assert code == 0 and len(transport.calls) == 5
    assert len({call[2] for call in transport.calls}) == 5
    assert [s["status"] for s in ledger_states(inputs)] == ["done"] * 5


def test_batch_interrupt_stops_dispatching(inputs, tmp_path, monkeypatch, capsys):
    """Ctrl+C lets the in-flight job finish and dispatches nothing new."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 3)
    entered, release = threading.Event(), threading.Event()

    class Blocking(Fake):
        def api(self, *a, **k):
            entered.set()
            release.wait(10)
            return super().api(*a, **k)

    transport, real_join, joins = Blocking([b64_image()] * 3), media._join, []

    def join(threads):
        joins.append(threads)
        if len(joins) == 1:
            entered.wait(10)
            raise KeyboardInterrupt
        release.set()
        real_join(threads)

    monkeypatch.setattr(media, "_join", join)
    code = media.main(["batch", str(jobs), "--project-dir", str(project(inputs)), "--execute"], transport=transport)
    assert code == 1 and len(transport.calls) == 1
    assert "interrupted by the user" in capsys.readouterr().err
    progress = json.loads((tmp_path / "jobs.progress.json").read_text(encoding="utf-8"))
    assert progress["remaining"] == ["j1", "j2"] and [r["status"] for r in progress["results"]] == ["generated"]


def test_batch_preview_reports_an_invalid_paid_cap(inputs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ledger_mod.MAX_PAID_ENV, "ten")
    jobs = write_jobs(tmp_path, 1)
    assert media.main(["batch", str(jobs), "--project-dir", str(project(inputs))], transport=Refuse()) == 0
    assert "whole number" in json.loads(capsys.readouterr().out)["warnings"][0]


def test_batch_rejects_bad_jobs_before_sending(inputs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    jobs = write_jobs(tmp_path, 2)
    for patch, message in (({"refrence": "x.png"}, "invalid job options"), ({"execute": True}, "whole batch"),
                           ({"prompt_file": "p0.txt"}, "identical requests")):
        data = json.loads(jobs.read_text(encoding="utf-8"))
        data["jobs"][1].update(patch)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(data), encoding="utf-8")
        assert media.main(["batch", str(bad), "--project-dir", str(project(inputs)), "--execute"], transport=Refuse()) == 1
        assert message in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# --- A4-T8: secret canaries ---------------------------------------------------

def leaky_scenarios(inputs, monkeypatch, capsys):
    """Fake transports that echo the key in result fields, errors, exceptions and
    poll results. Returns (stdout, stderr) of every CLI run."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("XAI_API_KEY", XAI_KEY)
    image, video = OPENAI_KEY, XAI_KEY
    ref = ["--reference", str(inputs[1])]
    runs = [
        ("image", "openai", [], Fake([{"data": [{"b64_json": base64.b64encode(png()).decode(), "revised_prompt": image}],
                                     "model": image, "usage": {"input_tokens": 3, "note": image}, "debug": image}],
                                   request_id=image)),
        ("image", "openai", [], Fake([media.MediaError(f"API HTTP 400: echoed {image}", code="invalid_request", sent=True,
                                                      provider={"httpStatus": 400, "message": f"bad key {image}"})])),
        ("image", "openai", [], Fake([RuntimeError(f"transport crashed with {image}")])),
        ("image", "openai", [], HTTPFailure(401, {"error": {"message": f"Incorrect API key provided: {image}",
                                                            "code": image, "param": image, "type": image}},
                                            {"x-request-id": image})),
        ("image", "openai", [], NetworkFailure(OSError(f"socket failure {image}"))),
        ("video", "xai", ref, Fake([{"request_id": "req-canary-1", "echo": video},
                                    {"status": "done", "model": video, "debug": video,
                                     "video": {"url": f"https://media.example/v.mp4?token={video}", "duration": video}}])),
        ("video", "xai", ref, Fake([{"request_id": video}])),
        ("video", "xai", ref, Fake([{"request_id": "req-canary-2"}, {"status": video, "error": video}])),
        ("video", "xai", ref, Fake([{"request_id": "req-canary-3"}, RuntimeError(f"poll failed {video}")])),
    ]
    out, err = [], []
    for index, (mode, provider, extra, transport) in enumerate(runs):
        (inputs[0].parent / f"prompt{index}.txt").write_text(f"Scenario {index}: a hero sprite", encoding="utf-8")
        command = argv(inputs, mode, provider, ["--execute", *extra], out=inputs[2].parent / f"job{index}")
        command[command.index("--prompt-file") + 1] = str(inputs[0].parent / f"prompt{index}.txt")
        media.main(command, transport=transport)
        captured = capsys.readouterr()
        out.append(captured.out)
        err.append(captured.err)
    return "".join(out), "".join(err)


@pytest.mark.parametrize("surface", ["stdout", "stderr", "job.json", "prompt.txt", "ledger"])
def test_secret_never_leaks(inputs, monkeypatch, capsys, surface):
    """jev-spell-wedding backend.test.mjs:151-179 pattern, per output surface."""
    stdout, stderr = leaky_scenarios(inputs, monkeypatch, capsys)
    root = inputs[0].parent
    texts = {"stdout": stdout, "stderr": stderr,
             "job.json": "".join(p.read_text(encoding="utf-8") for p in root.glob("job*/job.json")),
             "prompt.txt": "".join(p.read_text(encoding="utf-8") for p in root.glob("job*/prompt.txt")),
             "ledger": (project(inputs) / ".forge" / "ledger.jsonl").read_text(encoding="utf-8")}
    text = texts[surface]
    assert text.strip(), f"{surface} is empty, so the canary check would prove nothing"
    for key in (OPENAI_KEY, XAI_KEY):
        assert key not in text and key[12:30] not in text
