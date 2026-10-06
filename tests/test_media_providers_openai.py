"""OpenAI image adapter (media_providers.OpenAIAdapter): request shape, credential header, errors, gating.

Offline: the real generate_media.Transport talks to MockHTTPS (test_media_providers_common). The shapes
follow the OpenAI Images API reference as checked on 2026-10-06; nothing here proves a live call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_media_providers_common import (KEYS, assert_contracts, assert_no_secret, b64png, http, ledger_lines, media_providers,  # noqa: F401
                                         no_network, png, project, run_media, run_route, surfaces)

API = "https://api.openai.com/v1"
KEY = KEYS["OPENAI_API_KEY"]
PROMPT = "The same fox knight. Idle in place, facing LEFT. Flat #FF00FF."


def image_reply(size=(64, 64)):
    return 200, {"created": 1, "data": [{"b64_json": b64png(size)}],
                 "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}, {"x-request-id": "req_openai_1"}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 10, 6))


def media_argv(*extra):
    return ["image", "--provider", "openai", "--model", "gpt-image-2.5-sunburst", "--prompt-file", "prompt.txt",
            "--out-dir", "out/x", "--project-dir", ".", *extra]


def test_generation_sends_the_documented_json(project, http, key, capsys):
    http.on("POST", API + "/images/generations", image_reply())
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--size", "1024x1024", "--tier", "hero",
                                  "--transparent", "--out-dir", "out/hero")
    assert code == 0, err
    assert list(result)[:5] == ["status", "route", "artifact", "sha256", "estimateUsd"]
    assert (result["route"], result["model"], result["label"]) == ("api:openai", "gpt-image-2.5-sunburst",
                                                                  "OpenAI API (gpt-image-2.5-sunburst)")
    sent, = http.requests
    assert (sent.method, sent.url) == ("POST", API + "/images/generations")
    assert sent.headers["authorization"] == "Bearer " + KEY
    assert sent.headers["content-type"] == "application/json" and len(sent.headers["x-client-request-id"]) == 32
    assert sent.json() == {"model": "gpt-image-2.5-sunburst", "prompt": PROMPT, "n": 1, "output_format": "png",
                           "size": "1024x1024", "background": "transparent", "quality": "high"}
    job = json.loads(Path(result["job"]).read_text(encoding="utf-8"))
    assert job["status"] == "done" and job["providerRequestId"] == "req_openai_1" and job["provider"] == "openai"
    assert job["estimate"]["usd"] == result["estimateUsd"] == 0.0527  # 2.5 high 1024x1024, output tokens only
    assert job["providerUsage"] == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
    assert job["capability"]["verifiedAt"] == "2026-10-06" and job["capability"]["docs"][0].startswith("https://")
    assert_contracts(job, project)
    assert Path(result["artifact"]).read_bytes() == png((64, 64), (30, 100, 180))
    line = ledger_lines(project)[-1]
    assert (line["status"], line["provider"], line["model"], line["sha256"]) == (
        "done", "openai", "gpt-image-2.5-sunburst", result["sha256"])
    assert_no_secret(*surfaces(project, "out/hero", json.dumps(result), err))


def test_reference_edit_is_multipart_with_every_reference_in_order(project, http, key, capsys):
    http.on("POST", API + "/images/edits", image_reply())
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--reference", "master.png",
                                  "--reference", "peer.png", "--reference", "wide.png", "--out-dir", "out/edit")
    assert code == 0, err
    sent, = http.requests
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert sent.headers["authorization"] == "Bearer " + KEY
    body = sent.body
    assert body.count(b'name="image[]"') == 3
    positions = [body.index((project / name).read_bytes()) for name in ("master.png", "peer.png", "wide.png")]
    assert positions == sorted(positions)  # the prompt names the references in this order
    for field in (b'name="model"\r\n\r\ngpt-image-2.5-sunburst', b'name="size"\r\n\r\n1024x1024',
                  b'name="output_format"\r\n\r\npng', b'name="n"\r\n\r\n1'):
        assert field in body
    assert b"response_format" not in body and b'name="background"' not in body and b'name="quality"' not in body
    assert result["estimateUsd"] is None  # quality auto has no verified price


@pytest.mark.parametrize("extra, message", [
    (["--model", "gpt-image-1"], "shuts down on 2026-10-23"),
    (["--model", "dall-e-3"], "was shut down on 2026-05-12"),
    (["--size", "1000x1000"], "divisible by 16"),
    (["--size", "512x512"], "655,360"),
    (["--size", "3840x1024"], "between 1:3 and 3:1"),
    (["--model", "gpt-image-2", "--quality", "xhigh"], "quality is one of"),
    (["--resolution", "1k"], "OpenAI uses --size"),
    (["--reference", "master.png"] * 17, "at most 16 reference images"),
])
def test_capability_gating_refuses_before_any_request(project, http, key, capsys, extra, message):
    code, out, err = run_media(capsys, *media_argv("--execute", *extra))
    assert code == 1 and message in err, err
    assert not http.requests and not (project / "out").exists() and not (project / ".forge").exists()


def test_a_retiring_model_still_works_with_a_warning(project, key, capsys):
    code, plan, err = run_media(capsys, *media_argv("--model", "gpt-image-1.5", "--size", "1024x1024"))
    assert code == 0, err
    assert any("gpt-image-1.5 shuts down on 2026-12-01; use gpt-image-2.5-sunburst" in w for w in plan["warnings"])
    assert plan["execution"] == "dry-run" and plan["options"]["model"] == "gpt-image-1.5"


@pytest.mark.parametrize("status, body, code", [
    (401, {"error": {"message": "Incorrect API key provided: sk-abc***wxyz", "type": "invalid_request_error",
                     "code": "invalid_api_key"}}, "auth"),
    (429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota",
                     "code": "credit_balance_exhausted"}}, "quota"),
    (429, {"error": {"message": "Rate limit reached for requests", "type": "requests", "code": "rate_limit_exceeded"}},
     "rate_limit"),
    (400, {"error": {"message": "Your request was rejected by the safety system", "type": "image_generation_user_error",
                     "code": "moderation_blocked"}}, "moderation"),
    (403, {"error": {"message": "Country, region, or territory not supported", "type": "request_forbidden",
                     "code": "unsupported_country_region_territory"}}, "entitlement"),
    (500, {"error": {"message": "The server had an error while processing your request"}}, "provider_error"),
])
def test_errors_map_to_asf_codes_and_are_never_retried(project, http, key, capsys, status, body, code):
    body = json.loads(json.dumps(body).replace("wxyz", KEY[-12:]))
    http.on("POST", API + "/images/generations", (status, body, {"x-request-id": "req_err_1"}))
    rc, out, err = run_media(capsys, *media_argv("--execute"))
    assert rc == 1 and out is None and err.startswith("error: API HTTP")
    assert len(http.requests) == 1  # a paid POST is never sent twice
    job = json.loads((project / "out" / "x" / "job.json").read_text(encoding="utf-8"))
    assert job["receipt"]["outcomeCode"] == code and job["error"]["httpStatus"] == status
    assert job["status"] == ("submit_unknown" if status >= 500 else "failed")
    assert ledger_lines(project)[-1]["status"] == ("unknown" if status >= 500 else "failed")
    assert_no_secret(*surfaces(project, "out/x", err))


def test_an_account_refusal_moves_on_to_gemini(project, http, key, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEYS["GEMINI_API_KEY"])
    http.on("POST", API + "/images/generations",
            (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}))
    gemini = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent"
    http.on("POST", gemini, (200, {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "image/png", "data": b64png()}}]}, "finishReason": "STOP"}]}))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--out-dir", "out/fallback")
    assert code == 0, err
    assert result["route"] == "api:gemini" and [a["code"] for a in result["attempts"]] == ["quota"]
    assert result["attempts"][0]["keptIn"] == "fallback.failed-api-openai"
    assert [s.url for s in http.requests] == [API + "/images/generations", gemini]
