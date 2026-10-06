"""Provider adapters (generate2dmedia media_providers.py): the shared harness and the cross-provider contract.

Every adapter test runs the REAL generate_media.Transport: only its HTTPS handler is replaced
(Transport._https_handler) by MockHTTPS, a urllib handler that answers from a script. So header
construction, the no-redirect API policy, the safe media-download redirect policy, HTTP error mapping
and JSON parsing are exercised exactly as in production, and no socket is ever opened (the autouse
guard below fails any connection attempt). The other test_media_providers_*.py modules import their
helpers from here. Nothing here is a live check of a provider: the request shapes are verified
against the vendors' documentation only (references/capabilities.json, verifiedAt 2026-10-06).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import email.message
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
from urllib import request, response as urlresponse

from PIL import Image
import pytest

from forge_testutils import SKILLS_DIR, assert_valid_contract

SCRIPTS = SKILLS_DIR / "generate2dmedia" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import generate_media  # noqa: E402  (the instance route_media itself imports)
import media_config  # noqa: E402
import media_ledger  # noqa: E402
import media_mp4  # noqa: E402
import media_providers  # noqa: E402
import route_media  # noqa: E402

CAPABILITIES = SKILLS_DIR / "generate2dmedia" / "references" / "capabilities.json"
KEYS = {
    "OPENAI_API_KEY": "sk-adapter-canary-" + "0123456789abcdef" * 2,
    "GEMINI_API_KEY": "AIza-gemini-canary-" + "1123456789abcdef" * 2,
    "GOOGLE_API_KEY": "AIza-google-canary-" + "2123456789abcdef" * 2,
    "XAI_API_KEY": "xai-adapter-canary-" + "3123456789abcdef" * 2,
    "ARK_API_KEY": "ark-adapter-canary-" + "4123456789abcdef" * 2,
    "FAL_KEY": "fal-key-id-canary-5123:" + "fal-secret-canary-" + "6123456789abcdef" * 2,
}
MP4_HEADER = b"\x00\x00\x00\x18ftypmp42"


# --------------------------------------------------------------------------- the mock HTTPS layer

@dataclass
class Sent:
    """One request as it would have gone on the wire."""

    method: str
    url: str
    headers: dict
    body: bytes | None

    def json(self):
        return json.loads(self.body)


@dataclass
class Route:
    method: str
    prefix: str
    replies: list
    exact: bool = False
    hits: list = field(default_factory=list)

    def matches(self, sent: Sent) -> bool:
        return sent.method == self.method and (sent.url == self.prefix if self.exact else sent.url.startswith(self.prefix))


class MockHTTPS(request.HTTPSHandler):
    """urllib HTTPS handler that answers from a script instead of the network.

    Replies are (status, body[, headers]) - body a dict or list (sent as JSON) or bytes - or a callable that
    takes the Sent request and returns one.
    The last reply of a route repeats, so a "pending" poll can answer any number of times."""

    connected = True  # Transport classifies pre-connection failures by this flag

    def __init__(self):
        super().__init__()
        self.routes = []
        self.requests = []

    def on(self, method, url, *replies, exact=False):
        self.routes.append(Route(method, url, list(replies), exact))
        return self

    def https_open(self, req):
        sent = Sent(req.get_method(), req.full_url, {k.lower(): v for k, v in req.header_items()}, req.data)
        self.requests.append(sent)
        for route in self.routes:
            if route.matches(sent):
                reply = route.replies[min(len(route.hits), len(route.replies) - 1)]
                route.hits.append(sent)
                if callable(reply):  # a responder sees the request (and the disk) at the moment it is sent
                    reply = reply(sent)
                if isinstance(reply, BaseException):
                    raise reply
                status, body, *rest = reply
                headers = rest[0] if rest else {}
                return _response(req, status, body, headers)
        raise AssertionError(f"unexpected request {sent.method} {sent.url}")

    def calls(self, method=None):
        return [s for s in self.requests if method is None or s.method == method]


def _response(req, status, body, headers):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    message = email.message.Message()
    content_type = "application/json" if not isinstance(body, bytes) else "application/octet-stream"
    message["Content-Type"] = headers.get("Content-Type", content_type)
    for name, value in headers.items():
        if name != "Content-Type":
            message[name] = value
    reply = urlresponse.addinfourl(io.BytesIO(payload), message, req.full_url, status)
    reply.msg = "mock"
    return reply


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test in these modules may open a socket."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def http(monkeypatch):
    """The scripted HTTPS layer; polling does not really wait."""
    mock = MockHTTPS()
    monkeypatch.setattr(generate_media.Transport, "_https_handler", lambda self: mock)
    monkeypatch.setattr(generate_media.time, "sleep", lambda seconds: None)
    return mock


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder as cwd, an empty user config folder, no key, no cap, no local CLI."""
    for name in (*media_config.KEY_VARIABLES, "CODEX_API_KEY", media_ledger.MAX_PAID_ENV, route_media.FAKE_ENV,
                 *media_ledger.SESSION_ENV.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    monkeypatch.setattr(route_media.cli_media, "resolve_route_cli", lambda cli: route_media.cli_media.RouteCli(
        [], route_media.forge_doctor.CliInfo(cli, problem="not found on PATH")))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    (root / "prompt.txt").write_text("The same fox knight. Idle in place, facing LEFT. Flat #FF00FF.", encoding="utf-8")
    (root / "master.png").write_bytes(png((64, 64)))
    (root / "peer.png").write_bytes(png((64, 64), (10, 200, 10)))
    (root / "wide.png").write_bytes(png((96, 64)))
    return root


def png(size=(32, 48), colour=(255, 0, 255), mode="RGB") -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGB" else (*colour, 255)).save(output, "PNG")
    return output.getvalue()


def b64png(size=(64, 64)) -> str:
    return base64.b64encode(png(size, (30, 100, 180))).decode("ascii")


def configure(**fields) -> Path:
    path = media_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def run_route(capsys, *argv):
    """route_media.main in-process with the real Transport (MockHTTPS): (exit code, JSON line or None, stderr)."""
    code = route_media.main(list(argv))
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) <= 1, out
    assert out.isascii() and err.isascii()
    return code, json.loads(lines[0]) if lines else None, err


def run_media(capsys, *argv):
    """generate_media.main in-process: (exit code, JSON or None, stderr)."""
    code = generate_media.main(list(argv))
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    return code, json.loads(lines[-1]) if lines else None, err


def ledger_lines(root: Path) -> list:
    return media_ledger.Ledger(root).lines()


def assert_contracts(job: dict, root: Path) -> None:
    """A done job.json is a job_v2 document and every ledger line a ledger_line_v1 record (vendored schema)."""
    assert_valid_contract(job, "media", "job_v2", skill="generate2dmedia")
    for line in ledger_lines(root):
        assert_valid_contract(line, "media", "ledger_line_v1", skill="generate2dmedia")


def assert_no_secret(*texts):
    for text in texts:
        for value in KEYS.values():
            for part in (value, *value.split(":")):
                assert part not in text, "a key leaked"


def surfaces(root: Path, out_dir: str, *extra: str) -> list:
    """Every text an attempt leaves behind: job.json, prompt.txt, the ledger and the given console text."""
    folder = root / out_dir
    texts = list(extra)
    for name in ("job.json", "prompt.txt"):
        if (folder / name).is_file():
            texts.append((folder / name).read_text(encoding="utf-8"))
    ledger = root / ".forge" / "ledger.jsonl"
    if ledger.is_file():
        texts.append(ledger.read_text(encoding="utf-8"))
    return texts


# --------------------------------------------------------------------------- MP4 fixtures

def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def trak(handler: bytes, offsets, wide=False) -> bytes:
    hdlr = box(b"hdlr", b"\0" * 8 + handler + b"\0" * 12 + b"track\0")
    entries = b"".join(struct.pack(">Q" if wide else ">I", o) for o in offsets)
    table = box(b"co64" if wide else b"stco", b"\0" * 4 + struct.pack(">I", len(offsets)) + entries)
    minf = box(b"minf", box(b"stbl", box(b"stsd", b"\0" * 8) + table))
    return box(b"trak", box(b"tkhd", b"\0" * 84) + box(b"mdia", box(b"mdhd", b"\0" * 24) + hdlr + minf))


CHUNKS = (b"VIDEO-CHUNK-ONE.", b"AUDIO-CHUNK-xxxx", b"VIDEO-CHUNK-TWO.")


def mp4(moov_first=True, audio=True, wide=False) -> bytes:
    """ftyp + moov (a video track and, optionally, a sound track) + mdat holding CHUNKS."""
    ftyp = box(b"ftyp", b"mp42" + b"\0\0\0\0" + b"mp42isom")
    mdat_payload = b"".join(CHUNKS)

    def layout(base):
        video = trak(b"vide", [base, base + 32], wide)
        sound = trak(b"soun", [base + 16], wide)
        return box(b"moov", box(b"mvhd", b"\0" * 100) + video + (sound if audio else b""))

    if moov_first:
        size = len(layout(0))
        moov = layout(len(ftyp) + size + 8)
        return ftyp + moov + box(b"mdat", mdat_payload)
    start = len(ftyp) + 8
    return ftyp + box(b"mdat", mdat_payload) + layout(start)


def video_offsets(data: bytes) -> list:
    """The chunk offsets of the video track of an MP4 built by mp4()."""
    top = list(media_mp4._boxes(data, 0, len(data)))
    moov = next(b for b in top if b[0] == b"moov")
    for box_ in media_mp4._boxes(data, moov[1] + moov[2], moov[3]):
        if box_[0] == b"trak" and media_mp4._handler(data, box_) == b"vide":
            width, first, count = media_mp4._offset_tables(data, box_)[0]
            return [struct.unpack_from(">I" if width == 4 else ">Q", data, first + i * width)[0] for i in range(count)]
    raise AssertionError("no video track")


# --------------------------------------------------------------------------- MP4 audio removal

@pytest.mark.parametrize("moov_first", [True, False])
@pytest.mark.parametrize("wide", [False, True])
def test_strip_audio_drops_the_sound_track_and_keeps_video_bytes(moov_first, wide):
    clip = mp4(moov_first=moov_first, wide=wide)
    silent, info = media_mp4.strip_audio(clip)
    assert info == {"audioTracks": 1, "removed": 1}
    assert media_mp4.scan(silent)["tracks"] == ["vide"]
    assert len(silent) == len(clip) - len(trak(b"soun", [0], wide))
    for offset, chunk in zip(video_offsets(silent), (CHUNKS[0], CHUNKS[2])):
        assert silent[offset:offset + 16] == chunk  # the shifted offsets still name the same samples


def test_strip_audio_leaves_a_silent_clip_untouched_and_refuses_odd_files():
    silent = mp4(audio=False)
    assert media_mp4.strip_audio(silent) == (silent, {"audioTracks": 0, "removed": 0})
    fragmented = mp4() + box(b"moof", b"\0" * 8)
    for bad in (b"<html>nope</html>", fragmented, mp4()[:60]):
        with pytest.raises(media_mp4.Mp4Error):
            media_mp4.strip_audio(bad)


def test_remove_audio_falls_back_and_never_loses_the_clip(monkeypatch):
    fragmented = mp4() + box(b"moof", b"\0" * 8)
    data, record = media_mp4.remove_audio(fragmented, ffmpeg="")
    assert data == fragmented and record["method"] is None and "fragmented" in record["reason"]
    assert record["rawSha256"] == generate_media.digest(fragmented)
    monkeypatch.setattr(media_mp4, "_ffmpeg_strip", lambda data, tool: mp4(audio=False))
    data, record = media_mp4.remove_audio(fragmented, ffmpeg="ffmpeg")
    assert data == mp4(audio=False) and record["method"] == "ffmpeg"


@pytest.mark.ffmpeg
def test_strip_audio_on_a_real_clip(tmp_path):
    """A real H.264 + AAC clip from ffmpeg loses its audio stream and still decodes."""
    from forge_testutils import require_ffmpeg

    require_ffmpeg()
    source = tmp_path / "with-audio.mp4"
    made = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=24:d=1",
                           "-f", "lavfi", "-i", "sine=frequency=440:d=1", "-shortest", "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(source)],
                          capture_output=True, timeout=120)
    if made.returncode != 0:
        pytest.skip("this ffmpeg cannot encode H.264 + AAC")
    silent, info = media_mp4.strip_audio(source.read_bytes())
    assert info["removed"] == 1
    target = tmp_path / "silent.mp4"
    target.write_bytes(silent)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(target)],
                           capture_output=True, timeout=60)
    assert [s["codec_type"] for s in json.loads(probe.stdout)["streams"]] == ["video"]
    decode = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(target), "-f", "null", "-"],
                            capture_output=True, timeout=60)
    assert decode.returncode == 0 and not decode.stderr.strip()


# --------------------------------------------------------------------------- capabilities.json and prices.json

def test_capabilities_are_complete_dated_and_consistent():
    caps = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    assert caps["schema"] == "generate2dmedia.capabilities.v1" and caps["verifiedAt"] == "2026-10-06"
    assert list(caps["providers"]) == list(media_providers.ORDER)
    for name, record in caps["providers"].items():
        assert record["keyEnv"] == list(media_config.PROVIDER_KEYS[name]) == list(
            route_media.forge_doctor.API_PROVIDERS[name])
        assert record["label"] == route_media.forge_doctor.API_LABEL[name]
        assert record["apiBase"].startswith("https://") and record["verifiedAt"] == "2026-10-06"
        assert record["docs"] and all(url.startswith("https://") for url in record["docs"])
        for kind, tiers in record["tiers"].items():
            for tier in media_providers.TIERS:
                model = tiers[tier]["model"]
                assert record["models"][model]["kind"] == kind, (name, kind, tier)
        for model, entry in record["models"].items():
            assert entry["verifiedAt"] == "2026-10-06" and entry["docs"][0].startswith("https://"), model
            assert entry["async"] in ("sync", "poll") and entry["cost_unit"] in ("image", "second"), model
            if entry["kind"] == "video":
                assert {"i2v", "last_frame", "durations", "resolutions", "fps", "aspect_1_1", "audio_off"} <= set(entry["video"])
                assert media_providers.durations(entry["video"]), model
            else:
                assert {"generate", "edit", "refs_max", "transparent_bg", "sizes"} <= set(entry["image"]), model
            if name == "fal":
                assert entry["fal"].get("first") or entry["fal"].get("refs"), model
    for slot, model in media_config.MODEL_DEFAULTS.items():
        provider, kind = slot.split("-")
        assert caps["providers"][provider]["tiers"][kind]["standard"]["model"] == model
    shutdowns = {row["model"]: row["shutdown"] for row in caps["retired"]}
    assert shutdowns["gpt-image-1"] == "2026-10-23" and shutdowns["veo-3.1-generate-preview"] == "2026-10-22"
    assert shutdowns["gemini-2.5-flash-image"] == "2026-10-02" and shutdowns["grok-imagine-image-quality"] == "2026-11-02"


def test_every_price_row_names_a_capability_record():
    prices = media_ledger.load_prices()
    assert prices["version"] == "2026-10-06"
    for row in prices["rows"]:
        assert row["model"] in media_providers.adapter(row["provider"]).models(), row
        assert row["verifiedAt"] == "2026-10-06" and row["source"].startswith("https://")


def test_retirement_follows_the_calendar(monkeypatch):
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 11, 30))
    problem, warning = media_providers.retirement("openai", "gpt-image-1.5")
    assert problem is None and "shuts down on 2026-12-01" in warning
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 12, 1))
    assert "was shut down on 2026-12-01" in media_providers.retirement("openai", "gpt-image-1.5")[0]
    monkeypatch.setattr(media_providers, "TODAY", media_providers.date(2026, 10, 6))
    assert "ASF no longer sends it" in media_providers.retirement("openai", "gpt-image-1")[0]  # dropped early
    assert media_providers.retirement("openai", "gpt-image-2.5-sunburst") == (None, None)


def test_the_capability_cli_lists_every_provider(project, capsys):
    assert media_providers.main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)["providers"]
    assert [row["provider"] for row in listed] == list(media_providers.ORDER)
    assert not any(row["configured"] for row in listed)
    assert listed[1]["keyEnv"] == ["GOOGLE_API_KEY", "GEMINI_API_KEY"]
    assert media_providers.main(["show", "fal", "fal-ai/veo3.1/first-last-frame-to-video"]) == 0
    assert json.loads(capsys.readouterr().out)["fal"]["aspect"] == {"field": "aspect_ratio", "pad": "16:9"}


# --------------------------------------------------------------------------- keys, order, models, options

def test_each_provider_has_its_own_keys_and_fal_key_halves_are_redacted(project, monkeypatch):
    monkeypatch.setenv("FAL_KEY", KEYS["FAL_KEY"])
    secrets = media_config.known_secrets()
    assert {KEYS["FAL_KEY"], *KEYS["FAL_KEY"].split(":")} <= set(secrets)
    assert media_config.configured() == {"openai": False, "gemini": False, "xai": False, "byteplus": False, "fal": True}
    assert media_config.api_key("byteplus") is None  # never another provider's key
    assert route_media.redact(f"echo {KEYS['FAL_KEY'].split(':')[1]}") == "echo [redacted]"


def every_key(monkeypatch, *names):
    for name in names or ("OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "ARK_API_KEY", "FAL_KEY"):
        monkeypatch.setenv(name, KEYS[name])


def test_the_default_route_order_and_the_users_preference(project, monkeypatch, capsys):
    every_key(monkeypatch)
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--references", "1")
    assert result["order"] == ["api:openai", "api:gemini", "api:xai", "api:byteplus", "api:fal", "local:codex-cli",
                               "local:grok-cli"]
    assert result["available"] == ["api:openai", "api:gemini", "api:xai", "api:byteplus", "api:fal"]
    code, result, _ = run_route(capsys, "resolve", "--kind", "video")
    assert result["order"] == ["api:xai", "api:byteplus", "api:fal", "local:grok-acp"] and result["pinsLastFrame"]
    configure(providers={"order": {"image": ["gemini", "nonsense"], "video": ["fal", "byteplus"]}})
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--references", "1")
    assert result["order"][:3] == ["api:gemini", "api:openai", "api:xai"] and result["route"] == "api:gemini"
    assert "providers.order: unknown name 'nonsense' ignored" in result["skipped"]
    code, result, _ = run_route(capsys, "resolve", "--kind", "video")
    assert result["order"] == ["api:fal", "api:byteplus", "api:xai", "local:grok-acp"]
    assert (result["route"], result["model"]) == ("api:fal", "fal-ai/kling-video/v3/pro/image-to-video")
    configure(providers={"order": ["grok-acp", "codex-cli"]})  # one list for both kinds; local routes may lead
    assert run_route(capsys, "resolve", "--kind", "image")[1]["order"][0] == "local:codex-cli"
    assert run_route(capsys, "resolve", "--kind", "video")[1]["order"][0] == "local:grok-acp"


def test_model_and_tier_select_within_auto(project, monkeypatch, capsys):
    every_key(monkeypatch, "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY")
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--model", "gemini-3-pro-image")
    assert (result["route"], result["available"]) == ("api:gemini", ["api:gemini"])
    assert "api:openai: does not list the model gemini-3-pro-image" in result["skipped"]
    assert any(reason.startswith("local:codex-cli: a local CLI chooses its own model") for reason in result["skipped"])
    code, result, err = run_route(capsys, "resolve", "--kind", "image", "--model", "no-such-model")
    assert code == 1 and "is not a verified image model" in err
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--route", "openai", "--model", "gpt-image-3")
    assert code == 0 and result["model"] == "gpt-image-3"  # an unverified image model on a named provider
    code, result, _ = run_route(capsys, "resolve", "--kind", "video", "--tier", "draft")
    assert (result["route"], result["model"], result["pinsLastFrame"]) == ("api:xai", "grok-imagine-video-1.5-lite", False)
    code, result, _ = run_route(capsys, "resolve", "--kind", "image", "--tier", "hero", "--route", "gemini")
    assert result["model"] == "gemini-3-pro-image"
    configure(models={"gemini-image-hero": "gemini-3.1-flash-image"})
    assert run_route(capsys, "resolve", "--kind", "image", "--tier", "hero", "--route", "gemini")[1]["model"] == \
        "gemini-3.1-flash-image"


def test_provider_options_reach_only_their_provider(project, http, monkeypatch, capsys):
    every_key(monkeypatch, "OPENAI_API_KEY")
    http.on("POST", "https://api.openai.com/v1/images/generations", (200, {"data": [{"b64_json": b64png()}]}))
    code, result, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--provider-option", "moderation=low",
                                  "--provider-option", 'fal:safety_tolerance="2"', "--provider-option", "openai:seed=7",
                                  "--out-dir", "out/opts")
    assert code == 0, err
    body = http.requests[0].json()
    assert body["moderation"] == "low" and body["seed"] == 7 and "safety_tolerance" not in body
    job = json.loads((project / "out" / "opts" / "job.json").read_text(encoding="utf-8"))
    assert job["options"]["moderation"] == "low" and job["options"]["seed"] == 7
    code, _, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--provider-option", "prompt=override",
                             "--out-dir", "out/bad")
    assert code == 1 and "prompt is set by ASF itself" in err
    code, _, err = run_route(capsys, "image", "--prompt-file", "prompt.txt", "--provider-option", "novalue",
                             "--out-dir", "out/bad")
    assert code == 1 and "KEY=VALUE" in err


# --------------------------------------------------------------------------- transport safety

def test_the_download_redirect_policy_rebuilds_requests_without_credentials():
    original = request.Request("https://generativelanguage.googleapis.com/v1beta/files/a:download?alt=media")
    original.add_unredirected_header("X-goog-api-key", KEYS["GEMINI_API_KEY"])
    original.add_header("Authorization", "Bearer " + KEYS["OPENAI_API_KEY"])
    original.add_header("Cookie", "session=1")
    follow = generate_media.SafeRedirect().redirect_request(original, None, 302, "Found", {}, "https://store.example/a.png")
    assert dict(follow.header_items()) == {"User-agent": generate_media.USER_AGENT}
    assert follow.full_url == "https://store.example/a.png" and generate_media.SafeRedirect.max_redirections == 3
    for target in ("http://store.example/a.png", "https://user:pw@store.example/a.png"):
        with pytest.raises(generate_media.MediaError, match="redirect"):
            generate_media.SafeRedirect().redirect_request(original, None, 302, "Found", {}, target)


def test_api_requests_carry_the_credential_as_an_unredirected_header(http):
    http.on("GET", "https://api.x.ai/v1/videos/r1", (200, {"status": "pending"}))
    meta = {"authHeader": "x-goog-api-key", "authScheme": ""}
    generate_media.Transport().api("GET", "https://api.x.ai/v1/videos/r1", "secret-value-123456", meta=meta)
    sent, = http.requests
    assert sent.headers["x-goog-api-key"] == "secret-value-123456" and "authorization" not in sent.headers
    assert meta["httpStatus"] == 200
    http.on("GET", "https://api.x.ai/v1/videos/r2", (302, b"", {"Location": "https://elsewhere.example/"}))
    with pytest.raises(generate_media.MediaError, match="redirect refused"):
        generate_media.Transport().api("GET", "https://api.x.ai/v1/videos/r2", "secret-value-123456")


SECRET_CASES = {  # provider -> (key variable, kind, the paid endpoint, extra route_media arguments)
    "openai": ("OPENAI_API_KEY", "image", "https://api.openai.com/v1/images/generations", []),
    "gemini": ("GOOGLE_API_KEY", "image",
               "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent", []),
    "xai": ("XAI_API_KEY", "video", "https://api.x.ai/v1/videos/generations", []),
    "byteplus": ("ARK_API_KEY", "video", "https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks", []),
    "fal": ("FAL_KEY", "video", "https://queue.fal.run/fal-ai/kling-video/v3/pro/image-to-video",
            ["--provider-option", "fal_upload=data"]),
}


@pytest.mark.parametrize("provider", list(SECRET_CASES))
def test_a_key_echoed_by_any_provider_never_reaches_an_output(project, http, monkeypatch, capsys, provider):
    variable, kind, url, extra = SECRET_CASES[provider]
    key = KEYS[variable]
    monkeypatch.setenv(variable, key)
    (project / "still.png").write_bytes(png((512, 512)))
    echo = {"error": {"message": f"bad credentials {key}", "code": key, "type": key, "status": key,
                      "details": [{"reason": key}]}, "detail": f"bad {key}"}
    http.on("POST", url, (400, echo, {"x-request-id": key, "X-Fal-Request-Id": key, "X-Fal-Error-Type": key}))
    argv = [kind, "--prompt-file", "prompt.txt", "--route", provider, "--out-dir", "out/canary", *extra]
    argv += ["--reference", "still.png"] if kind == "video" else []
    code, result, err = run_route(capsys, *argv)
    assert code == 1 and err.strip(), err
    sent, = http.requests
    assert key in json.dumps(sent.headers)  # the key was sent ...
    assert_no_secret(*surfaces(project, "out/canary", err, json.dumps(result)))  # ... and never shown or stored


# --------------------------------------------------------------------------- padding and the doctor

def test_padding_uses_the_still_backdrop_and_records_a_resolution_free_crop():
    green = png((90, 160), (0, 255, 0))
    meta = {"size": [90, 160], "mime": "image/png", "sha256": "0" * 64, "bytes": len(green)}
    padded, transform, crop = media_providers.pad_to_aspect([(meta, green)], "16:9")
    with Image.open(io.BytesIO(padded[0][1])) as image:
        assert image.size == (286, 160) and image.getpixel((1, 1)) == (0, 255, 0)
    assert transform == {"type": "pad", "aspect": "16:9", "source": [90, 160], "canvas": [286, 160], "offset": [98, 0],
                         "fill": "#00FF00"}
    assert crop["x"] == round(98 / 286, 6) and crop["width"] == round(90 / 286, 6) and crop["height"] == 1.0
    clear = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(clear, "PNG")
    padded, transform, _ = media_providers.pad_to_aspect([({**meta, "size": [64, 64]}, clear.getvalue())], "16:9")
    assert transform["fill"] == "#FF00FF"  # no flat opaque backdrop: the default key colour


def test_the_doctor_lists_every_provider_and_the_resolved_order(project, tmp_path, monkeypatch):
    doctor = route_media.forge_doctor
    (tmp_path / "empty-bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    for name in ("FORGE_CODEX_EXE", "FORGE_GROK_EXE"):
        monkeypatch.delenv(name, raising=False)
    every_key(monkeypatch, "ARK_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", KEYS["GOOGLE_API_KEY"])
    configure(providers={"order": ["byteplus"]})
    report = doctor.diagnose(host_tools="none", project_dir=project, run_versions=False, console_encoding="utf-8")
    assert report["apiKeys"] == {"openai": False, "gemini": True, "xai": False, "byteplus": True, "fal": False}
    assert report["providers"]["gemini"]["variable"] == "GOOGLE_API_KEY"
    assert report["providers"]["fal"] == {"label": "fal.ai API", "configured": False, "keyEnv": ["FAL_KEY"],
                                          "source": None, "variable": None, "kinds": ["image", "video"]}
    assert [r for r in report["routeOrder"]["image"] if r.startswith("api:")] == ["api:byteplus", "api:gemini"]
    assert [r for r in report["routeOrder"]["video"] if r.startswith("api:")] == ["api:byteplus"]
    assert report["providerPreference"] == {"image": ["byteplus"], "video": ["byteplus"]}
    assert "GOOGLE_API_KEY is configured" in next(c for c in report["checks"] if c["id"] == "media.api.gemini")["detail"]
    text = doctor.render_text(report)
    assert "openai=no  gemini=yes  xai=no  byteplus=yes  fal=no" in text
    assert "preference  image: byteplus (providers.order in the user config file)" in text
    assert_valid_contract(report, "media", "doctor_v1", skill="generate2dmedia")
    assert_no_secret(json.dumps(report), text)
