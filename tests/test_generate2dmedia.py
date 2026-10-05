"""Offline provider-contract tests. These do not claim live provider quality."""
import base64
import importlib.util
import io
import json
from pathlib import Path

from PIL import Image
import pytest

SOURCE = Path(__file__).parents[1] / "skills/generate2dmedia/scripts/generate_media.py"
spec = importlib.util.spec_from_file_location("forge_media", SOURCE)
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def png():
    output = io.BytesIO()
    Image.new("RGBA", (32, 48), (30, 100, 180, 128)).save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def inputs(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Preserve this hero's silhouette. Idle in place.", encoding="utf-8")
    ref = tmp_path / "base.png"
    ref.write_bytes(png())
    return prompt, ref, tmp_path / "output"


def args(inputs, mode="image", provider="openai", extra=()):
    prompt, ref, out = inputs
    model = "gpt-image-2.5-sunburst" if provider == "openai" else (
        "grok-imagine-video-1.5" if mode == "video" else "grok-imagine-image-2.0")
    return media.parser().parse_args([mode, "--provider", provider, "--model", model,
        "--prompt-file", str(prompt), "--out-dir", str(out), *extra])


class Fake:
    def __init__(self, results=(), download=b"\x00\x00\x00\x18ftypmp42data"):
        self.results = iter(results)
        self.calls = []
        self.downloads = []
        self.content = download

    def api(self, method, url, key, body=None, content_type=None, timeout=90):
        self.calls.append((method, url, body, content_type))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result

    def download(self, url, timeout=90):
        self.downloads.append(url)
        return self.content


def test_dry_run_never_uses_credentials_or_network(inputs, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    transport = Fake()
    result = media.execute(args(inputs), transport)
    assert result["execution"] == "dry-run"
    assert result["paidRequests"] == 1
    assert not inputs[2].exists() and not transport.calls
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
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    transport = Fake()
    with pytest.raises(media.MediaError, match="already exists"):
        media.execute(args(inputs, extra=["--execute"]), transport)
    assert not transport.calls


def test_image_saved_with_actual_format_and_hash(inputs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    transport = Fake([{"data": [{"b64_json": base64.b64encode(png()).decode()}]}])
    result = media.execute(args(inputs, extra=["--execute"]), transport)
    assert result["status"] == "done"
    assert (inputs[2] / "generated.png").read_bytes() == png()
    assert result["artifact"]["hasAlphaChannel"] is True
    assert result["returnedModel"] is None  # do not fabricate server model metadata
    assert "secret-test-key" not in (inputs[2] / "job.json").read_text()
    assert len(transport.calls) == 1


def test_unknown_post_outcome_never_retries(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "secret-test-key")
    transport = Fake([media.MediaError("network failure")])
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute"])
    with pytest.raises(media.MediaError):
        media.execute(a, transport)
    job = json.loads((inputs[2] / "job.json").read_text())
    assert job["status"] == "submit_unknown" and len(transport.calls) == 1
    r = media.parser().parse_args(["resume", "--job", str(inputs[2] / "job.json")])
    with pytest.raises(media.MediaError, match="request ID"):
        media.resume(r, transport)
    assert len(transport.calls) == 1


def test_video_resume_only_polls_and_downloads(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "secret-test-key")
    first = Fake([{"request_id": "request-123"}, media.MediaError("poll disconnected")])
    a = args(inputs, "video", "xai", ["--reference", str(inputs[1]), "--execute"])
    with pytest.raises(media.MediaError):
        media.execute(a, first)
    assert [c[0] for c in first.calls] == ["POST", "GET"]
    transport = Fake([{"status": "done", "model": "grok-imagine-video-1.5", "video": {
        "url": "https://media.example/clip.mp4?token=PRIVATE", "duration": 4}}])
    path = inputs[2] / "job.json"
    r = media.parser().parse_args(["resume", "--job", str(path)])
    result = media.resume(r, transport)
    assert result["status"] == "done" and [c[0] for c in transport.calls] == ["GET"]
    assert "PRIVATE" not in path.read_text()
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
    inputs[2].mkdir()
    with pytest.raises(media.MediaError, match="MP4"):
        media.write_artifact(inputs[2], b"<html>error</html>", "video")
    with pytest.raises(media.MediaError, match="image"):
        media.write_artifact(inputs[2], b"bad png", "image")
    assert not list(inputs[2].iterdir())


def test_api_redirect_does_not_forward_key():
    with pytest.raises(media.MediaError, match="redirect"):
        media.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


def test_resume_recovers_artifact_published_before_job_commit(inputs, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "secret-test-key")
    inputs[2].mkdir()
    content = b"\x00\x00\x00\x18ftypmp42data"
    (inputs[2] / "generated.mp4").write_bytes(content)
    path = inputs[2] / "job.json"
    media.save_json(path, {"schemaVersion": 1, "provider": "xai", "kind": "video",
                          "status": "downloading", "requestId": "req-recover"})
    transport = Fake([{"status": "done", "video": {"url": "https://media.example/clip.mp4"}}])
    result = media.resume(media.parser().parse_args(["resume", "--job", str(path)]), transport)
    assert result["status"] == "done"
    assert [call[0] for call in transport.calls] == ["GET"]
    assert (inputs[2] / "generated.mp4").read_bytes() == content


def test_recovery_preserves_conflicting_existing_artifact(inputs):
    inputs[2].mkdir()
    original = b"\x00\x00\x00\x18ftypmp42original"
    (inputs[2] / "generated.mp4").write_bytes(original)
    with pytest.raises(media.MediaError, match="already exists"):
        media.write_artifact(inputs[2], b"\x00\x00\x00\x18ftypmp42different", "video", adopt_identical=True)
    assert (inputs[2] / "generated.mp4").read_bytes() == original
