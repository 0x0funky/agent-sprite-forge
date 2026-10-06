"""BytePlus ModelArk adapter (media_providers.BytePlusAdapter): Seedream images and Seedance video under one
ARK_API_KEY: request shapes, the task poll, error codes and gating.

Offline: the real generate_media.Transport talks to MockHTTPS (test_media_providers_common). The shapes follow
the ModelArk image-generation and video-generation API pages as checked on 2026-10-06; nothing here proves a
live call.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from test_media_providers_common import (KEYS, assert_contracts, assert_no_secret, b64png, http, ledger_lines, media_mp4,  # noqa: F401
                                         mp4, no_network, png, project, run_media, run_route, surfaces)

API = "https://ark.ap-southeast.bytepluses.com/api/v3"
KEY = KEYS["ARK_API_KEY"]
VIDEO = "https://ark-content-generation-ap-southeast-1.tos-ap-southeast-1.bytepluses.com/out/clip.mp4?X-Tos-Signature=S"


@pytest.fixture
def key(monkeypatch, project):
    """ARK_API_KEY, plus still.png: a 512 px master (Seedance takes first frames of 300 to 6000 px)."""
    monkeypatch.setenv("ARK_API_KEY", KEY)
    (project / "still.png").write_bytes(png((512, 512)))


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def test_seedream_image_request_shape(project, http, key, capsys):
    http.on("POST", API + "/images/generations", (200, {"model": "dola-seedream-5-0-pro-260628", "created": 1,
                                                        "data": [{"b64_json": b64png(), "size": "1024x1024"}],
                                                        "usage": {"generated_images": 1, "output_tokens": 4096,
                                                                  "total_tokens": 4096}}))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--reference", "peer.png", "--out-dir", "out/master")
    assert code == 0, err
    assert (result["route"], result["model"]) == ("api:byteplus", "dola-seedream-5-0-pro-260628")
    sent, = http.requests
    assert (sent.method, sent.url) == ("POST", API + "/images/generations")
    assert sent.headers["authorization"] == "Bearer " + KEY
    body = sent.json()
    assert body == {"model": "dola-seedream-5-0-pro-260628", "prompt": body["prompt"], "response_format": "b64_json",
                    "output_format": "png", "watermark": False, "size": "1024x1024",
                    "image": [data_uri(project / "master.png"), data_uri(project / "peer.png")]}
    assert result["estimateUsd"] == pytest.approx(0.045 + 0.003)  # the first reference is free
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert job["options"]["image"] == ["<image>", "<image>"] and job["returnedModel"] == "dola-seedream-5-0-pro-260628"
    assert_no_secret(*surfaces(project, "out/master", json.dumps(result), err))


def test_seedream_raises_a_small_size_and_drafts_on_flash(project, http, key, capsys):
    http.on("POST", API + "/images/generations", (200, {"data": [{"b64_json": b64png()}]}))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--route", "byteplus", "--size",
                                  "512x512", "--tier", "draft", "--out-dir", "out/small")
    assert code == 0, err
    body = http.requests[0].json()
    assert body["model"] == "dola-seedream-5-0-flash-260915" and body["size"] == "960x960" and "image" not in body
    assert any("raised to 960x960" in note for note in result["notes"]) and result["estimateUsd"] == 0.018


def test_seedance_pins_first_and_last_frames_square_and_silent(project, http, key, capsys):
    http.on("POST", API + "/contents/generations/tasks", (200, {"id": "cgt-20261006-abc"}))
    http.on("GET", API + "/contents/generations/tasks/cgt-20261006-abc", (200, {"id": "cgt-20261006-abc", "status": "queued"}),
            (200, {"status": "running"}),
            (200, {"status": "succeeded", "model": "dreamina-seedance-2-0-260128", "duration": 6, "ratio": "1:1",
                   "content": {"video_url": VIDEO}, "usage": {"completion_tokens": 72900, "total_tokens": 72900}}))
    http.on("GET", "https://ark-content-generation-", (200, mp4()))
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "byteplus", "--reference",
                                  "still.png", "--last-frame", "still.png", "--out-dir", "out/idle")
    assert code == 0, err
    assert (result["model"], result["lastFrameUsed"]) == ("dreamina-seedance-2-0-260128", True)
    post = http.calls("POST")[0]
    body = post.json()
    first = data_uri(project / "still.png")
    assert body == {"model": "dreamina-seedance-2-0-260128", "ratio": "1:1", "duration": 6, "resolution": "720p",
                    "generate_audio": False, "watermark": False, "content": [
                        {"type": "text", "text": body["content"][0]["text"]},
                        {"type": "image_url", "image_url": {"url": first}, "role": "first_frame"},
                        {"type": "image_url", "image_url": {"url": first}, "role": "last_frame"}]}
    download = http.calls("GET")[-1]
    assert download.url == VIDEO and "authorization" not in download.headers
    folder = project / "out" / "idle"
    assert media_mp4.scan((folder / "generated.mp4").read_bytes())["tracks"] == ["vide"]
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    assert job["requestId"] == "cgt-20261006-abc" and job["options"]["frames"] == ["first_frame", "last_frame"]
    assert job["providerUsage"] == {"output_tokens": 72900, "total_tokens": 72900}  # billed by completion tokens
    assert result["estimateUsd"] == pytest.approx(6 * 0.15) and "square clips cost less" in job["estimate"]["basis"]
    assert "X-Tos-Signature" not in json.dumps(job)
    assert_contracts(job, project)
    line = ledger_lines(project)[-1]
    assert (line["status"], line["jobId"], line["sha256"]) == ("done", "cgt-20261006-abc", result["sha256"])


def test_seedance_hero_follows_the_input_and_short_takes_are_snapped(project, http, key, capsys):
    http.on("POST", API + "/contents/generations/tasks", (200, {"id": "cgt-hero"}))
    http.on("GET", API + "/contents/generations/tasks/cgt-hero",
            (200, {"status": "succeeded", "content": {"video_url": VIDEO}}))
    http.on("GET", "https://ark-content-generation-", (200, mp4(audio=False)))
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "byteplus", "--tier", "hero",
                                  "--reference", "still.png", "--duration", "2", "--out-dir", "out/hero")
    assert code == 0, err
    body = http.calls("POST")[0].json()
    assert (body["model"], body["ratio"], body["duration"]) == ("dreamina-seedance-2-5-260628", "adaptive", 4)
    assert result["duration"] == 4 and any("renders 4 s" in note for note in result["notes"])
    assert [item["role"] for item in body["content"][1:]] == ["first_frame"]


@pytest.mark.parametrize("status, code_text, code", [
    (401, "AuthenticationError", "auth"),
    (403, "AccountOverdueError", "quota"),
    (429, "SetLimitExceeded", "quota"),
    (429, "RateLimitExceeded.EndpointRPMExceeded", "rate_limit"),
    (400, "InputImageSensitiveContentDetected.PrivacyInformation", "moderation"),
    (404, "ModelNotOpen", "entitlement"),
    (403, "OperationDenied.ServiceNotOpen", "entitlement"),
    (400, "InvalidParameter", "invalid_request"),
    (500, "InternalServiceError", "provider_error"),
])
def test_error_codes_map_to_asf_codes(project, http, key, capsys, status, code_text, code):
    http.on("POST", API + "/images/generations", (status, {"error": {"code": code_text, "message": f"{code_text}. Request id: 0217",
                                                                     "param": "", "type": "Unauthorized"}}))
    rc, out, err = run_media(capsys, "image", "--provider", "byteplus", "--model", "dola-seedream-5-0-pro-260628",
                             "--prompt-file", "prompt.txt", "--out-dir", "out/err", "--project-dir", ".", "--execute")
    assert rc == 1 and len(http.requests) == 1
    job = json.loads((project / "out" / "err" / "job.json").read_text(encoding="utf-8"))
    assert job["receipt"]["outcomeCode"] == code and job["error"]["code"] == code_text


@pytest.mark.parametrize("task, status, code, ledger", [
    ({"status": "failed", "error": {"code": "OutputVideoSensitiveContentDetected", "message": "blocked"}},
     "failed", "moderation", "unknown"),
    ({"status": "failed", "error": {"code": "InternalServiceError", "message": "boom"}}, "failed", "provider_error",
     "failed"),
    ({"status": "expired"}, "expired", "expired", "failed"),
    ({"status": "cancelled"}, "failed", "failed", "failed"),
])
def test_a_failed_task_is_final(project, http, key, capsys, task, status, code, ledger):
    http.on("POST", API + "/contents/generations/tasks", (200, {"id": "cgt-fail"}))
    http.on("GET", API + "/contents/generations/tasks/cgt-fail", (200, task))
    rc, out, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "byteplus", "--reference",
                             "still.png", "--out-dir", "out/fail")
    assert rc == 1 and err.startswith(f"error: api:byteplus: {code}:") and len(http.calls("POST")) == 1
    job = json.loads((project / "out" / "fail" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == status and ledger_lines(project)[-1]["status"] == ledger


def test_gating_first_frame_limits_and_resolutions(project, http, key, capsys):
    (project / "tiny.png").write_bytes(png((200, 200)))
    code, out, err = run_media(capsys, "video", "--provider", "byteplus", "--model", "dreamina-seedance-2-0-260128",
                               "--prompt-file", "prompt.txt", "--reference", "tiny.png", "--out-dir", "out/x",
                               "--project-dir", ".", "--execute")
    assert code == 1 and "300..6000 px per side" in err
    code, out, err = run_media(capsys, "video", "--provider", "byteplus", "--model", "dreamina-seedance-2-0-mini-260615",
                               "--prompt-file", "prompt.txt", "--reference", "still.png", "--resolution", "1080p",
                               "--out-dir", "out/x", "--project-dir", ".", "--execute")
    assert code == 1 and "renders 480p, 720p" in err
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "byteplus", "--reference",
                                  "tiny.png", "--out-dir", "out/y")
    assert code == 3 and "300..6000 px per side" in result["skipped"][0]
    code, out, err = run_media(capsys, "image", "--provider", "byteplus", "--model", "dola-seedream-5-0-pro-260628",
                               "--prompt-file", "prompt.txt", "--transparent", "--out-dir", "out/x", "--project-dir", ".")
    assert code == 1 and "flat key colour" in err
    assert not http.requests
