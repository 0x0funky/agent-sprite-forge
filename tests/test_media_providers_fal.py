"""fal.ai adapter (media_providers.FalAdapter): CDN uploads before the paid request, the queue (submit, status,
result), the curated parameter maps (Kling, Veo with 16:9 padding and a recorded crop, Luma loop, LTX static
camera, MiniMax, Wan, Vidu, image edits), failures and gating.

Offline: the real generate_media.Transport talks to MockHTTPS (test_media_providers_common). The shapes follow
fal.ai's queue docs, the official fal_client upload flow and each model's API page as checked on 2026-10-06;
nothing here proves a live call.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from test_media_providers_common import (KEYS, assert_contracts, assert_no_secret, http, ledger_lines, media_mp4,  # noqa: F401
                                         mp4, no_network, png, project, run_media, run_route, surfaces)

QUEUE = "https://queue.fal.run"
KEY = KEYS["FAL_KEY"]
CDN_TOKEN = "cdn-token-canary-" + "7123456789abcdef" * 2
TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
UPLOAD_URL = "https://v3.fal.media/files/upload"
OUTPUT = "https://v3.fal.media/files/penguin/out-clip.mp4"


@pytest.fixture
def key(monkeypatch, project):
    monkeypatch.setenv("FAL_KEY", KEY)
    (project / "still.png").write_bytes(png((512, 512)))


def queue(http, model, request_id="fal-req-1", status=(("IN_QUEUE",), ("IN_PROGRESS",), ("COMPLETED",)), result=None,
          submit_status=200, status_replies=None):
    """Script the queue of one request: submit, the status answers (or status_replies verbatim), the result
    and the media."""
    base = f"{QUEUE}/{model.split('/')[0]}/{model.split('/')[1]}/requests/{request_id}"
    http.on("POST", f"{QUEUE}/{model}", (submit_status, {"request_id": request_id, "status_url": base + "/status",
                                                         "response_url": base, "cancel_url": base + "/cancel",
                                                         "queue_position": 0}), exact=True)
    replies = status_replies or [(200, {"status": s[0], **(s[1] if len(s) > 1 else {})}) for s in status]
    http.on("GET", base + "/status", *replies, exact=True)
    http.on("GET", base, result or (200, {"video": {"url": OUTPUT, "content_type": "video/mp4"}}), exact=True)
    http.on("GET", OUTPUT, (200, mp4()), exact=True)
    return base


def cdn(http):
    uploads = []

    def upload(sent):
        uploads.append(sent)
        return 200, {"access_url": f"https://v3.fal.media/files/input/{len(uploads)}.png"}
    http.on("POST", TOKEN_URL, (200, {"token": CDN_TOKEN, "token_type": "Bearer", "base_url": "https://v3.fal.media",
                                      "expires_at": "2026-10-06T12:00:00+00:00"}), exact=True)
    http.on("POST", UPLOAD_URL, upload, exact=True)
    return uploads


def decode(uri: str) -> Image.Image:
    assert uri.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


def test_kling_uploads_inputs_first_then_queues_polls_and_downloads(project, http, key, capsys):
    uploads = cdn(http)
    model = "fal-ai/kling-video/v3/pro/image-to-video"
    queue(http, model)
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "fal", "--reference",
                                  "still.png", "--last-frame", "still.png", "--out-dir", "out/run")
    assert code == 0, err
    assert (result["route"], result["model"], result["lastFrameUsed"]) == ("api:fal", model, True)
    urls = [s.url for s in http.requests]
    assert urls[:3] == [TOKEN_URL, UPLOAD_URL, UPLOAD_URL] and urls[3] == f"{QUEUE}/{model}"  # free uploads first
    token, *_ = http.requests
    assert token.headers["authorization"] == "Key " + KEY and token.body == b"{}"
    for index, sent in enumerate(uploads):
        assert sent.headers["authorization"] == "Bearer " + CDN_TOKEN and sent.headers["content-type"] == "image/png"
        assert sent.body == (project / "still.png").read_bytes()
        assert sent.headers["x-fal-file-name"] == ("first-frame.png", "last-frame.png")[index]
    submit = http.requests[3]
    assert submit.headers["authorization"] == "Key " + KEY
    assert submit.json() == {"prompt": submit.json()["prompt"], "start_image_url": "https://v3.fal.media/files/input/1.png",
                             "end_image_url": "https://v3.fal.media/files/input/2.png", "duration": "6",
                             "generate_audio": False}
    polls = [s for s in http.requests if s.url.endswith("/status")]
    assert len(polls) == 3 and all(s.headers["authorization"] == "Key " + KEY for s in polls)
    media = http.requests[-1]
    assert media.url == OUTPUT and "authorization" not in media.headers
    folder = project / "out" / "run"
    assert media_mp4.scan((folder / "generated.mp4").read_bytes())["tracks"] == ["vide"]
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    assert job["requestId"] == "fal-req-1" and job["queue"]["statusUrl"].endswith("/requests/fal-req-1/status")
    assert job["uploads"] == {"to": "v3.fal.media", "count": 2}
    assert job["options"]["start_image_url"] == "<image>"
    assert_contracts(job, project)
    assert result["estimateUsd"] == pytest.approx(6 * 0.112)
    line = ledger_lines(project)[-1]
    assert (line["status"], line["jobId"], line["sha256"]) == ("done", "fal-req-1", result["sha256"])
    assert any("renders at its own resolution" in n for n in result["notes"])
    texts = surfaces(project, "out/run", json.dumps(result), err)
    assert_no_secret(*texts)
    assert not any(CDN_TOKEN in text for text in texts)


def test_veo_pads_to_16_9_with_the_key_colour_and_records_the_crop(project, http, key, capsys):
    model = "fal-ai/veo3.1/first-last-frame-to-video"
    queue(http, model, "fal-veo")
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", f"fal:{model}",
                                  "--reference", "master.png", "--last-frame", "master.png", "--provider-option",
                                  "fal:fal_upload=data", "--provider-option", 'fal:safety_tolerance="5"',
                                  "--out-dir", "out/veo")
    assert code == 0, err
    body = http.calls("POST")[0].json()
    assert (body["aspect_ratio"], body["duration"], body["resolution"], body["generate_audio"]) == ("16:9", "6s", "720p",
                                                                                                    False)
    assert body["safety_tolerance"] == "5"
    first = decode(body["first_frame_url"])
    assert first.size == (114, 64) and first.getpixel((5, 5)) == (255, 0, 255)  # the master's own key colour
    assert body["first_frame_url"] == body["last_frame_url"]
    crop = result["crop"]
    assert crop == json.loads((project / "out" / "veo" / "job.json").read_text(encoding="utf-8"))["crop"]
    assert (crop["x"], crop["y"], crop["width"], crop["height"]) == (round(25 / 114, 6), 0.0, round(64 / 114, 6), 1.0)
    job = json.loads((project / "out" / "veo" / "job.json").read_text(encoding="utf-8"))
    assert job["inputTransform"] == {"type": "pad", "aspect": "16:9", "source": [64, 64], "canvas": [114, 64],
                                     "offset": [25, 0], "fill": "#FF00FF"}
    assert result["estimateUsd"] == pytest.approx(6 * 0.20)


def test_veo_needs_both_frames(project, http, key, capsys):
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route",
                                  "fal:fal-ai/veo3.1/first-last-frame-to-video", "--reference", "master.png",
                                  "--out-dir", "out/veo")
    assert code == 3 and "needs a last frame" in result["skipped"][0] and not http.requests


def test_luma_turns_an_identical_last_frame_into_its_own_loop(project, http, key, capsys):
    model = "luma/agent/ray/v3.2/image-to-video"
    queue(http, model, "fal-luma")
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", f"fal:{model}",
                                  "--reference", "master.png", "--last-frame", "master.png", "--provider-option",
                                  "fal_upload=data", "--out-dir", "out/luma")
    assert code == 0, err
    body = http.calls("POST")[0].json()
    assert body["loop"] is True and "end_image_url" not in body
    assert (body["duration"], body["resolution"], body["aspect_ratio"]) == ("5s", "720p", "1:1")
    assert result["duration"] == 5 and result["estimateUsd"] == pytest.approx(5 * 0.06)


def test_ltx_sends_a_static_camera_and_pads(project, http, key, capsys):
    model = "lightricks/ltx-2.5/image-to-video/pro"
    queue(http, model, "fal-ltx")
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", f"fal:{model}",
                                  "--reference", "master.png", "--provider-option", "fal_upload=data",
                                  "--out-dir", "out/ltx")
    assert code == 0, err
    body = http.calls("POST")[0].json()
    assert (body["camera_motion"], body["fps"], body["aspect_ratio"], body["duration"], body["generate_audio"]) == (
        "static", 24, "16:9", 6, False)
    assert decode(body["image_url"]).size == (114, 64) and "crop" in result


@pytest.mark.parametrize("model, resolution, expected", [
    ("minimax/h3/image-to-video", "720p", {"image_url": "<data>", "end_image_url": "<data>", "duration": 6,
                                            "resolution": "768P", "prompt_expansion_mode": "disabled"}),
    ("alibaba/wan-3.0/image-to-video", "720p", {"start_image_url": "<data>", "end_image_url": "<data>", "duration": 6,
                                                 "resolution": "720p", "aspect_ratio": "1:1", "audio": False,
                                                 "enable_prompt_expansion": False}),
    ("fal-ai/vidu/q3/image-to-video", "480p", {"image_url": "<data>", "end_image_url": "<data>", "duration": 6,
                                                "resolution": "540p", "audio": False}),
])
def test_curated_parameter_maps(project, http, key, capsys, model, resolution, expected):
    queue(http, model, "fal-map")
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", f"fal:{model}",
                                  "--reference", "master.png", "--last-frame", "master.png", "--resolution", resolution,
                                  "--provider-option", "fal_upload=data", "--out-dir", "out/map")
    assert code == 0, err
    body = http.calls("POST")[0].json()
    body.pop("prompt")
    assert {k: ("<data>" if isinstance(v, str) and v.startswith("data:") else v) for k, v in body.items()} == expected


def test_image_edit_uploads_every_reference(project, http, key, capsys):
    uploads = cdn(http)
    model = "fal-ai/nano-banana-2/edit"
    queue(http, model, "fal-img", result=(200, {"images": [{"url": "https://v3.fal.media/files/out/master.png",
                                                            "width": 64, "height": 64}]}))
    http.on("GET", "https://v3.fal.media/files/out/master.png", (200, png((64, 64), (1, 2, 3))), exact=True)
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--route", "fal", "--reference",
                                  "master.png", "--reference", "peer.png", "--out-dir", "out/img")
    assert code == 0, err
    assert len(uploads) == 2 and [u.headers["x-fal-file-name"] for u in uploads] == ["reference-0.png", "reference-1.png"]
    body = http.calls("POST")[-1].json()
    assert body["image_urls"] == ["https://v3.fal.media/files/input/1.png", "https://v3.fal.media/files/input/2.png"]
    assert (body["aspect_ratio"], body["resolution"], body["num_images"], body["output_format"]) == ("1:1", "1K", 1, "png")
    assert Path(result["artifact"]).read_bytes() == png((64, 64), (1, 2, 3)) and result["estimateUsd"] == 0.08


def test_fal_image_models_are_edits_only(project, http, key, capsys):
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--route", "fal", "--out-dir", "out/x")
    assert code == 3 and "add at least 1 --reference" in result["skipped"][0] and not http.requests


@pytest.mark.parametrize("status, result, code, ledger", [
    ((("IN_PROGRESS",), ("COMPLETED", {"error": "Content policy violation", "error_type": "content_policy_violation"})),
     None, "moderation", "unknown"),
    ((("COMPLETED",),), (422, {"detail": [{"loc": ["body", "prompt"], "msg": "The content could not be processed",
                                           "type": "content_policy_violation"}]}), "moderation", "unknown"),
    ((("COMPLETED",),), (422, {"detail": [{"loc": ["body"], "msg": "No media generated", "type": "no_media_generated"}]}),
     "failed", "failed"),
])
def test_a_completed_request_with_an_error_is_final(project, http, key, capsys, status, result, code, ledger):
    model = "fal-ai/kling-video/v3/pro/image-to-video"
    queue(http, model, "fal-err", status=status, result=result)
    rc, out, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "fal", "--reference",
                             "still.png", "--provider-option", "fal_upload=data", "--out-dir", "out/err")
    assert rc == 1 and err.startswith(f"error: api:fal: {code}:"), err
    assert len(http.calls("POST")) == 1 and not http.calls("GET")[-1].url == OUTPUT
    job = json.loads((project / "out" / "err" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "failed" and ledger_lines(project)[-1]["status"] == ledger


def test_an_upload_failure_means_the_paid_request_was_never_sent(project, http, key, capsys, monkeypatch):
    http.on("POST", TOKEN_URL, (401, {"detail": "Invalid key"}), exact=True)
    rc, out, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "fal", "--reference",
                             "still.png", "--out-dir", "out/up")
    assert rc == 1 and err.startswith("error: api:fal: auth: fal.ai input upload failed before the paid request")
    assert [s.url for s in http.requests] == [TOKEN_URL]  # nothing reached the queue
    job = json.loads((project / "out" / "up" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "not_sent" and ledger_lines(project)[-1]["status"] == "not_sent"


@pytest.mark.parametrize("status, body, code", [
    (401, {"detail": "Unauthorized"}, "auth"),
    (403, {"detail": "User is locked. Reason: Exhausted balance. Top up your balance at fal.ai/dashboard/billing."}, "quota"),
    (404, {"detail": "Application 'kling-video' not found"}, "entitlement"),
    (422, {"detail": [{"loc": ["body", "duration"], "msg": "unexpected value", "type": "value_error"}]}, "invalid_request"),
    (429, {"detail": "Too many requests"}, "rate_limit"),
])
def test_queue_submit_errors_map_to_asf_codes(project, http, key, capsys, status, body, code):
    model = "fal-ai/kling-video/v3/pro/image-to-video"
    http.on("POST", f"{QUEUE}/{model}", (status, body, {"X-Fal-Request-Id": "fal-rid-9"}), exact=True)
    rc, out, err = run_media(capsys, "video", "--provider", "fal", "--model", model, "--prompt-file", "prompt.txt",
                             "--reference", "still.png", "--provider-option", "fal_upload=data", "--out-dir", "out/q",
                             "--project-dir", ".", "--execute", "--duration", "6")
    assert rc == 1 and len(http.requests) == 1
    job = json.loads((project / "out" / "q" / "job.json").read_text(encoding="utf-8"))
    assert job["receipt"]["outcomeCode"] == code and job["providerRequestId"] == "fal-rid-9"
    if status == 422:
        assert (job["error"]["param"], job["error"]["type"]) == ("duration", "value_error")


def test_queue_urls_on_another_host_are_never_polled(project, http, key, capsys):
    model = "fal-ai/kling-video/v3/pro/image-to-video"
    http.on("POST", f"{QUEUE}/{model}", (200, {"request_id": "fal-evil", "status_url": "https://evil.example/status",
                                               "response_url": "https://evil.example/result"}), exact=True)
    rc, out, err = run_media(capsys, "video", "--provider", "fal", "--model", model, "--prompt-file", "prompt.txt",
                             "--reference", "still.png", "--provider-option", "fal_upload=data", "--out-dir", "out/e",
                             "--project-dir", ".", "--execute", "--duration", "6")
    assert rc == 1 and "never rebuilt" in err
    assert [s.url for s in http.requests] == [f"{QUEUE}/{model}"]  # the key never went to evil.example


@pytest.mark.parametrize("argv, message", [
    (["--route", "fal:fal-ai/unknown/model"], "is not a verified fal.ai API video model"),
    (["--route", "fal:fal-ai/nano-banana-2/edit"], "makes images, not videos"),
    (["--route", "codex-cli", "--model", "x"], "not a video route"),
    (["--model", "fal-ai/unknown/model"], "is not a verified video model"),
])
def test_explicit_models_are_checked(project, http, key, capsys, argv, message):
    try:
        code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--reference", "still.png",
                                      "--out-dir", "out/m", *argv)
    except SystemExit as stop:  # a route that does not exist for video is a usage error
        assert stop.code == 2 and message in capsys.readouterr().err
        return
    assert code == 1 and message in err and not http.requests


def test_provider_options_cannot_override_what_asf_sets(project, http, key, capsys):
    code, result, err = run_route(capsys, "video", "--prompt-file", "prompt.txt", "--route", "fal", "--reference",
                                  "still.png", "--provider-option", "fal:start_image_url=https://x.example/a.png",
                                  "--out-dir", "out/o")
    assert code == 1 and "start_image_url is set by ASF itself" in err
    assert not http.requests and not (project / "out").exists()


def test_an_interrupted_queue_resumes_from_the_stored_urls(project, http, key, capsys):
    model = "fal-ai/kling-video/v3/pro/image-to-video"
    base = queue(http, model, "fal-resume", status_replies=[(503, {"detail": "Service unavailable"}),
                                                              (200, {"status": "COMPLETED"})])
    rc, out, err = run_media(capsys, "video", "--provider", "fal", "--model", model, "--prompt-file", "prompt.txt",
                             "--reference", "still.png", "--provider-option", "fal_upload=data", "--duration", "5",
                             "--out-dir", "out/r", "--project-dir", ".", "--execute")
    assert rc == 1 and "503" in err
    job = json.loads((project / "out" / "r" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "interrupted" and job["queue"]["responseUrl"] == base
    rc, job, err = run_media(capsys, "resume", "--job", "out/r/job.json")
    assert rc == 0, err
    assert job["status"] == "done" and len(http.calls("POST")) == 1  # resume never submits again
    assert [s.url for s in http.calls("GET")][-2:] == [base, OUTPUT]
    assert ledger_lines(project)[-1]["jobId"] == "fal-resume"
