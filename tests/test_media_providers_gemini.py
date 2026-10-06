"""Google Gemini image adapter (media_providers.GeminiAdapter): request shape, key precedence, safety blocks,
the file-download redirect, errors and gating.

Offline: the real generate_media.Transport talks to MockHTTPS (test_media_providers_common). The shapes follow
the Gemini API generateContent reference and image-generation guide as checked on 2026-10-06; nothing here
proves a live call.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from test_media_providers_common import (KEYS, assert_contracts, assert_no_secret, b64png, configure, generate_media, http,  # noqa: F401
                                         ledger_lines, media_providers, no_network, png, project, route_media, run_media,
                                         run_route, surfaces)

BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
FLASH = BASE + "gemini-3.1-flash-image:generateContent"
PROMPT = "The same fox knight. Idle in place, facing LEFT. Flat #FF00FF."


def reply(*parts, finish="STOP", **extra):
    return 200, {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}],
                 "usageMetadata": {"promptTokenCount": 1290, "candidatesTokenCount": 1120, "thoughtsTokenCount": 64,
                                   "totalTokenCount": 2474}, "modelVersion": "gemini-3.1-flash-image",
                 "responseId": "resp-1", **extra}


def image_part(size=(64, 64), thought=False):
    part = {"inlineData": {"mimeType": "image/png", "data": b64png(size)}}
    if thought:
        part["thought"] = True
    return part


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEYS["GEMINI_API_KEY"])
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 10, 6))


def test_request_shape_key_header_and_the_final_image(project, http, key, capsys):
    """References are inline parts after the text, in order; the key is x-goog-api-key (never Authorization);
    interim thought images are skipped."""
    http.on("POST", FLASH, reply(image_part((32, 32), thought=True), {"text": "Here it is"}, image_part((64, 64))))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--reference", "peer.png", "--size", "1024x1024", "--out-dir", "out/master")
    assert code == 0, err
    assert (result["route"], result["model"]) == ("api:gemini", "gemini-3.1-flash-image")
    sent, = http.requests
    assert (sent.method, sent.url) == ("POST", FLASH)
    assert sent.headers["x-goog-api-key"] == KEYS["GEMINI_API_KEY"] and "authorization" not in sent.headers
    body = sent.json()
    parts = body["contents"][0]["parts"]
    assert body["contents"][0]["role"] == "user" and parts[0] == {"text": PROMPT}
    assert [base64.b64decode(p["inlineData"]["data"]) for p in parts[1:]] == [
        (project / "master.png").read_bytes(), (project / "peer.png").read_bytes()]
    assert {p["inlineData"]["mimeType"] for p in parts[1:]} == {"image/png"}
    assert body["generationConfig"] == {"responseModalities": ["IMAGE"],
                                        "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}}
    assert Path(result["artifact"]).read_bytes() == png((64, 64), (30, 100, 180))  # not the 32 px thought image
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert job["returnedModel"] == "gemini-3.1-flash-image" and job["keyEnv"] == "GOOGLE_API_KEY or GEMINI_API_KEY"
    assert job["providerUsage"] == {"input_tokens": 1290, "output_tokens": 1120, "thought_tokens": 64,
                                    "total_tokens": 2474}
    assert result["estimateUsd"] == 0.067 and "input image tokens are not included" in job["estimate"]["basis"]
    assert_contracts(job, project)
    assert ledger_lines(project)[-1]["sha256"] == result["sha256"]
    assert_no_secret(*surfaces(project, "out/master", json.dumps(result), err))


def test_google_api_key_wins_and_the_environment_beats_the_file(project, http, monkeypatch, capsys):
    http.on("POST", FLASH, reply(image_part()))
    monkeypatch.setenv("GEMINI_API_KEY", KEYS["GEMINI_API_KEY"])
    monkeypatch.setenv("GOOGLE_API_KEY", KEYS["GOOGLE_API_KEY"])
    assert run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/a")[0] == 0
    assert http.requests[-1].headers["x-goog-api-key"] == KEYS["GOOGLE_API_KEY"]
    monkeypatch.delenv("GOOGLE_API_KEY")
    configure(GOOGLE_API_KEY="AIza-config-file-google-0123456789")
    assert run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/b")[0] == 0
    assert http.requests[-1].headers["x-goog-api-key"] == KEYS["GEMINI_API_KEY"]  # environment before the file
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", KEYS["OPENAI_API_KEY"])  # another provider's key is never borrowed
    configure()
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--route", "gemini")
    assert code == 3 and result["skipped"] == ["api:gemini: no GOOGLE_API_KEY or GEMINI_API_KEY in the environment "
                                               "or the user config file"]


@pytest.mark.parametrize("tier, model", [("hero", "gemini-3-pro-image"), ("draft", "gemini-3.1-flash-lite-image")])
def test_tiers_pick_pro_for_hero_masters_and_lite_for_drafts(project, http, key, capsys, tier, model):
    http.on("POST", BASE + model + ":generateContent", reply(image_part()))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--tier", tier, "--size", "2048x2048", "--out-dir", "out/tier")
    assert code == 0, err
    assert result["model"] == model
    config = http.requests[-1].json()["generationConfig"]["imageConfig"]
    assert config["imageSize"] == ("2K" if tier == "hero" else "1K")  # lite renders 1K only
    if tier == "hero":
        assert result["estimateUsd"] == pytest.approx(0.134 + 0.0011)  # pro prices its input image


@pytest.mark.parametrize("answer, code", [
    ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "moderation"),
    ({"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]}, "moderation"),
    ({"candidates": [{"finishReason": "NO_IMAGE", "content": {"parts": [{"text": "I cannot draw that"}]}}]}, "failed"),
])
def test_a_withheld_image_is_a_final_refusal(project, http, key, capsys, monkeypatch, answer, code):
    monkeypatch.setenv("XAI_API_KEY", KEYS["XAI_API_KEY"])
    http.on("POST", FLASH, (200, answer))
    rc, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/blocked")
    assert rc == 1 and result is None and err.startswith(f"error: api:gemini: {code}:")
    assert len(http.requests) == 1  # never retried, never handed to xAI
    job = json.loads((project / "out" / "blocked" / "job.json").read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["receipt"]["outcomeCode"] == code
    assert ledger_lines(project)[-1]["status"] == "failed"


def test_a_file_download_sends_the_key_only_to_the_api_host_never_across_the_redirect(project, http, key, capsys):
    file_uri = "https://generativelanguage.googleapis.com/v1beta/files/abc123:download?alt=media"
    storage = "https://storage.googleapis.example/bucket/abc123.png?signature=SIGNED"
    http.on("POST", FLASH, reply({"fileData": {"mimeType": "image/png", "fileUri": file_uri}}))
    http.on("GET", file_uri, (302, b"", {"Location": storage}), exact=True)
    http.on("GET", storage, (200, png((40, 40)), {"Content-Type": "image/png"}), exact=True)
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/file")
    assert code == 0, err
    first, second = http.calls("GET")
    assert first.url == file_uri and first.headers["x-goog-api-key"] == KEYS["GEMINI_API_KEY"]
    assert second.url == storage and "x-goog-api-key" not in second.headers and "authorization" not in second.headers
    assert Path(result["artifact"]).read_bytes() == png((40, 40))
    assert "SIGNED" not in (project / "out" / "file" / "job.json").read_text(encoding="utf-8")


def test_a_file_on_another_host_is_fetched_without_the_key(project, http, key, capsys):
    other = "https://cdn.example/generated/abc.png"
    http.on("POST", FLASH, reply({"fileData": {"mimeType": "image/png", "fileUri": other}}))
    http.on("GET", other, (200, png((40, 40))), exact=True)
    assert run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/other")[0] == 0
    assert "x-goog-api-key" not in http.calls("GET")[0].headers


def test_a_redirect_to_plain_http_is_refused(project, http, key, capsys):
    file_uri = "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"
    http.on("POST", FLASH, reply({"fileData": {"mimeType": "image/png", "fileUri": file_uri}}))
    http.on("GET", file_uri, (302, b"", {"Location": "http://insecure.example/abc.png"}), exact=True)
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/http")
    assert code == 1 and "redirect" in err
    assert len(http.calls("GET")) == 1


@pytest.mark.parametrize("status, body, code", [
    (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}, "auth"),
    (402, {"error": {"code": 402, "message": "Prepay credits are used up", "status": "RESOURCE_EXHAUSTED"}}, "quota"),
    (429, {"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                     "status": "RESOURCE_EXHAUSTED"}}, "rate_limit"),
    (403, {"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}},
     "entitlement"),
    (400, {"error": {"code": 400, "message": "User location is not supported for the API use.",
                     "status": "FAILED_PRECONDITION"}}, "entitlement"),
    (404, {"error": {"code": 404, "message": "models/x is not found", "status": "NOT_FOUND"}}, "entitlement"),
    (400, {"error": {"code": 400, "message": "Invalid value at 'generation_config'", "status": "INVALID_ARGUMENT"}},
     "invalid_request"),
    (503, {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}, "provider_error"),
])
def test_errors_map_to_asf_codes(project, http, key, capsys, status, body, code):
    http.on("POST", FLASH, (status, body))
    rc, out, err = run_media(capsys, "image", "--provider", "gemini", "--model", "gemini-3.1-flash-image",
                             "--prompt-file", "prompt.txt", "--out-dir", "out/err", "--project-dir", ".", "--execute")
    assert rc == 1 and len(http.requests) == 1
    job = json.loads((project / "out" / "err" / "job.json").read_text(encoding="utf-8"))
    assert job["receipt"]["outcomeCode"] == code
    assert set(job["error"]) <= {"httpStatus", "code", "type", "param", "message", "requestId"}
    if status == 400 and code == "auth":
        assert (job["error"]["code"], job["error"]["type"]) == ("API_KEY_INVALID", "INVALID_ARGUMENT")


@pytest.mark.parametrize("extra, message", [
    (["--model", "gemini-2.5-flash-image"], "was shut down on 2026-10-02"),
    (["--model", "veo-3.1-generate-preview"], "ASF no longer sends it"),
    (["--model", "imagen-4.0-generate-001"], "was shut down on 2026-08-17"),
    (["--transparent"], "no native transparent background"),
    (["--quality", "high"], "no quality option"),
    (["--model", "gemini-3-pro-image", "--resolution", "512"], "renders 1K, 2K, 4K"),
    (["--reference", "master.png"] * 15, "at most 14 reference images"),
])
def test_capability_gating(project, http, key, capsys, extra, message):
    code, out, err = run_media(capsys, "image", "--provider", "gemini", "--model", "gemini-3.1-flash-image",
                               "--prompt-file", "prompt.txt", "--out-dir", "out/x", "--project-dir", ".", "--execute",
                               *extra)
    assert code == 1 and message in err, err
    assert not http.requests and not (project / "out").exists()


def test_gemini_never_serves_video(project, key, capsys):
    """Veo through the Gemini API is not added (its previews shut down 2026-10-22)."""
    with pytest.raises(SystemExit) as usage:
        generate_media.main(["video", "--provider", "gemini", "--model", "veo-3.1-generate-preview", "--prompt-file",
                             "prompt.txt", "--reference", "master.png", "--out-dir", "out/v"])
    assert usage.value.code == 2
    capsys.readouterr()
    code, result, _ = run_route(capsys, "resolve", "--kind", "video")
    assert code == 3 and not any(reason.startswith("api:gemini") for reason in result["skipped"])
    assert "api:gemini" not in route_media.ORDER["video"] and media_providers.adapter("gemini").kinds == ("image",)
