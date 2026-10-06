"""xAI adapter (media_providers.XAIAdapter): images (multi-reference edits) and video (last_frame, keyframes,
silent generation, the 1.5-lite draft tier), the poll loop, local audio removal, errors and gating.

Offline: the real generate_media.Transport talks to MockHTTPS (test_media_providers_common). The shapes follow
the xAI REST reference and video guides as checked on 2026-10-06; nothing here proves a live call.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from test_media_providers_common import (KEYS, assert_contracts, assert_no_secret, b64png, http, ledger_lines,  # noqa: F401
                                         media_mp4, media_providers, mp4, no_network, project, run_media, run_route,
                                         surfaces)

API = "https://api.x.ai/v1"
KEY = KEYS["XAI_API_KEY"]
MEDIA = "https://vidgen.x.ai/xai-vidgen-bucket/clip.mp4?token=TEMPORARY"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 10, 6))


def done(cost_ticks=8_600_000_000):
    return 200, {"status": "done", "model": "grok-imagine-video-1.5", "progress": 100,
                 "video": {"url": MEDIA, "duration": 6, "respect_moderation": True},
                 "usage": {"cost_in_usd_ticks": cost_ticks}}


def test_video_pins_last_frame_and_keyframes_silently_then_strips_audio(project, http, key, capsys):
    seen = {}

    def first_poll(sent):  # the job id is on disk before the first poll
        seen["job"] = json.loads((project / "out" / "idle" / "job.json").read_text(encoding="utf-8"))
        return 202, {"status": "pending", "progress": 10}

    http.on("POST", API + "/videos/generations", (200, {"request_id": "req-video-1"}))
    http.on("GET", API + "/videos/req-video-1", first_poll, (200, {"status": "pending", "progress": 60}), done())
    http.on("GET", "https://vidgen.x.ai/", (200, mp4()))
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--last-frame", "master.png", "--keyframe", "master.png@2.5", "--duration", "6",
                                  "--resolution", "720p", "--out-dir", "out/idle", "--timeout", "60")
    assert code == 0, err
    assert (result["route"], result["model"], result["lastFrameUsed"]) == ("api:xai", "grok-imagine-video-1.5", True)
    post = http.calls("POST")[0]
    assert post.headers["authorization"] == "Bearer " + KEY
    body = post.json()
    master = "data:image/png;base64," + base64.b64encode((project / "master.png").read_bytes()).decode()
    assert body == {"model": "grok-imagine-video-1.5", "prompt": body["prompt"], "image": {"url": master},
                    "duration": 6, "resolution": "720p", "generate_audio": False, "last_frame": {"url": master},
                    "keyframes": [{"image": {"url": master}, "timestamp_s": 2.5}]}
    assert "aspect_ratio" not in body  # image-to-video follows the still; an override would stretch it
    assert seen["job"]["requestId"] == "req-video-1" and seen["job"]["status"] == "pending"
    download = http.calls("GET")[-1]
    assert download.url == MEDIA and "authorization" not in download.headers  # no key for the media host
    folder = project / "out" / "idle"
    assert media_mp4.scan((folder / "generated.mp4").read_bytes())["tracks"] == ["vide"]
    assert (folder / "provider-download.mp4").read_bytes() == mp4()  # the download itself is kept
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    assert job["audio"]["removed"] == 1 and job["audio"]["method"] == "mp4-boxes"
    assert job["audio"]["raw"]["path"] == "provider-download.mp4" and job["costUsd"] == 0.86
    assert job["options"]["keyframes"] == [2.5] and job["keyframes"][0]["seconds"] == 2.5
    assert_contracts(job, project)
    assert result["estimateUsd"] == pytest.approx(6 * 0.14 + 3 * 0.01)  # first, last and one keyframe image
    line = ledger_lines(project)[-1]
    assert (line["status"], line["jobId"], line["sha256"], line["actualUsd"]) == (
        "done", "req-video-1", result["sha256"], 0.86)
    assert "TEMPORARY" not in json.dumps(job) and len(http.calls("POST")) == 1
    assert_no_secret(*surfaces(project, "out/idle", json.dumps(result), err))


def test_the_draft_tier_uses_lite_and_drops_what_it_cannot_take(project, http, key, capsys):
    http.on("POST", API + "/videos/generations", (200, {"request_id": "req-lite"}))
    http.on("GET", API + "/videos/req-lite", done())
    http.on("GET", "https://vidgen.x.ai/", (200, mp4(audio=False)))
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--last-frame", "master.png", "--keyframe", "master.png@1", "--tier", "draft",
                                  "--resolution", "480p", "--out-dir", "out/draft")
    assert code == 0, err
    assert result["model"] == "grok-imagine-video-1.5-lite" and result["lastFrameUsed"] is False
    assert any("last frame is not pinned" in n for n in result["notes"])
    assert any("keyframes are not sent" in n for n in result["notes"])
    body = http.calls("POST")[0].json()
    assert not {"last_frame", "keyframes", "generate_audio"} & set(body)  # undocumented for lite: never sent
    assert result["estimateUsd"] == pytest.approx(6 * 0.02 + 0.01)
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert job["audio"]["removed"] == 0 and "raw" not in job["audio"]  # a silent clip is published as is


def test_multi_reference_image_edit_is_json(project, http, key, capsys):
    http.on("POST", API + "/images/edits", (200, {"data": [{"b64_json": b64png(), "mime_type": "image/png"}]}))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--route", "xai", "--reference",
                                  "master.png", "--reference", "peer.png", "--reference", "wide.png",
                                  "--out-dir", "out/edit")
    assert code == 0, err
    sent, = http.requests
    assert sent.headers["content-type"] == "application/json"
    body = sent.json()
    assert [item["type"] for item in body["images"]] == ["image_url"] * 3 and "image" not in body
    assert body["images"][1]["url"] == "data:image/png;base64," + base64.b64encode(
        (project / "peer.png").read_bytes()).decode()
    assert (body["resolution"], body["response_format"], body["n"]) == ("1k", "b64_json", 1)
    assert "aspect_ratio" not in body  # an edit keeps the reference's shape
    assert result["estimateUsd"] == pytest.approx(0.06 + 3 * 0.01)  # auto quality on an edit is priced as medium


@pytest.mark.parametrize("status, body, code", [
    (400, {"code": "invalid-argument", "error": "Incorrect API key provided. You can obtain an API key from https://console.x.ai."},
     "auth"),
    (401, {"code": "unauthenticated:no-credentials", "error": "No credentials presented."}, "auth"),
    (404, {"code": "not-found", "error": "The model grok-imagine-video-1.5 does not exist or your team does not have access to it."},
     "entitlement"),
    (429, {"code": "resource-exhausted", "error": "Too many requests: rate limit reached"}, "rate_limit"),
    (403, {"code": "permission-denied", "error": "Your team has run out of credits"}, "quota"),
])
def test_submit_errors_map_to_asf_codes(project, http, key, capsys, status, body, code):
    http.on("POST", API + "/videos/generations", (status, body))
    rc, out, err = run_media(capsys, "video", "--provider", "xai", "--model", "grok-imagine-video-1.5", "--prompt-file",
                             "prompt.txt", "--reference", "master.png", "--out-dir", "out/err", "--project-dir", ".",
                             "--execute")
    assert rc == 1 and len(http.requests) == 1
    job = json.loads((project / "out" / "err" / "job.json").read_text(encoding="utf-8"))
    assert job["receipt"]["outcomeCode"] == code and job["status"] == "failed"


@pytest.mark.parametrize("poll, status, code, ledger", [
    ({"status": "failed", "error": {"code": "invalid_argument", "message": "Content blocked by moderation"}},
     "failed", "moderation", "unknown"),
    ({"status": "done", "video": {"url": "", "respect_moderation": False}}, "failed", "moderation", "unknown"),
    ({"status": "expired"}, "expired", "expired", "failed"),
    ({"status": "failed", "error": {"code": "internal_error", "message": "boom"}}, "failed", "failed", "failed"),
])
def test_terminal_jobs_are_never_resubmitted(project, http, key, capsys, poll, status, code, ledger):
    http.on("POST", API + "/videos/generations", (200, {"request_id": "req-term"}))
    http.on("GET", API + "/videos/req-term", (200, poll))
    rc, out, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "master.png",
                             "--out-dir", "out/term")
    assert rc == 1 and err.startswith(f"error: api:xai: {code}:")
    assert len(http.calls("POST")) == 1
    job = json.loads((project / "out" / "term" / "job.json").read_text(encoding="utf-8"))
    assert (job["status"], job["requestId"]) == (status, "req-term")
    assert (ledger_lines(project)[-1]["status"], ledger_lines(project)[-1]["jobId"]) == (ledger, "req-term")
    code_resume, _, err = run_media(capsys, "resume", "--job", "out/term/job.json")
    assert code_resume == 1 and "terminal" in err and len(http.calls("POST")) == 1


def test_an_interrupted_poll_resumes_without_paying_again(project, http, key, capsys):
    http.on("POST", API + "/videos/generations", (200, {"request_id": "req-resume"}))
    http.on("GET", API + "/videos/req-resume", (503, {"code": "unavailable", "error": "try later"}), done())
    http.on("GET", "https://vidgen.x.ai/", (200, mp4()))
    rc, _, err = run_media(capsys, "video", "--provider", "xai", "--model", "grok-imagine-video-1.5", "--prompt-file",
                           "prompt.txt", "--reference", "master.png", "--out-dir", "out/r", "--project-dir", ".",
                           "--execute")
    assert rc == 1 and "503" in err
    job = json.loads((project / "out" / "r" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "interrupted" and ledger_lines(project)[-1]["status"] == "reserved"
    rc, job, err = run_media(capsys, "resume", "--job", "out/r/job.json")
    assert rc == 0, err
    assert job["status"] == "done" and len(http.calls("POST")) == 1
    assert ledger_lines(project)[-1]["status"] == "done" and ledger_lines(project)[-1]["jobId"] == "req-resume"


@pytest.mark.parametrize("extra, message", [
    (["--resolution", "1080p", "--last-frame", "master.png"], "pins a last frame only at 480p or 720p"),
    (["--resolution", "1080p", "--keyframe", "master.png@2"], "keyframes only at 480p or 720p"),
    (["--keyframe", "master.png@0"], "strictly between 0 and --duration"),
    (["--keyframe", "master.png@1", "--keyframe", "master.png@1.2"], "1/3 s apart"),
    (["--keyframe", "wide.png@2"], "same canvas size"),
    (["--keyframe", "master.png"], "PATH@SECONDS"),
    (["--model", "grok-imagine-video", "--last-frame", "master.png"], "cannot pin a last frame"),
    (["--model", "grok-imagine-video-1.5-lite", "--keyframe", "master.png@2"], "takes no keyframes"),
    (["--duration", "16"], "renders 1..15 seconds"),
    (["--model", "grok-imagine-video-9"], "Unsupported video model"),
])
def test_video_capability_gating(project, http, key, capsys, extra, message):
    code, out, err = run_media(capsys, "video", "--provider", "xai", "--model", "grok-imagine-video-1.5",
                               "--prompt-file", "prompt.txt", "--reference", "master.png", "--duration", "6",
                               "--out-dir", "out/x", "--project-dir", ".", "--execute", *extra)
    assert code == 1 and message in err, err
    assert not http.requests and not (project / "out").exists()


@pytest.mark.parametrize("extra, message", [
    (["--quality", "high"], "requires image-2.0 and low/medium/auto"),
    (["--size", "1024x1024"], "no transparency switch"),
    (["--reference", "master.png"] * 6, "at most 5 reference images"),
    (["--model", "grok-imagine-image", "--reference", "master.png", "--reference", "peer.png"], "at most 1 reference"),
    (["--model", "grok-imagine-image-quality"], "shuts down on 2026-11-02"),
])
def test_image_capability_gating(project, http, key, capsys, extra, message):
    code, out, err = run_media(capsys, "image", "--provider", "xai", "--model", "grok-imagine-image-2.0",
                               "--prompt-file", "prompt.txt", "--out-dir", "out/x", "--project-dir", ".", *extra)
    if "shuts down" in message:  # a retiring model plans with a warning (dry run)
        assert code == 0 and any(message in w for w in out["warnings"]), err
        return
    assert code == 1 and message in err, err
