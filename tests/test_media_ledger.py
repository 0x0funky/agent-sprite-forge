"""Spend ledger, price table and fingerprints (generate2dmedia/scripts/media_ledger.py).

Offline: no transport is ever real; generate_media is driven with fakes.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from PIL import Image
import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills/generate2dmedia/scripts"
spec = importlib.util.spec_from_file_location("forge_media_for_ledger_tests", SCRIPTS / "generate_media.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)
ml = media.media_ledger  # the instance generate_media uses

KEY = "xai-ledger-canary-" + "0123456789abcdef" * 2
FP = "ab" * 32


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "XAI_API_KEY", ml.MAX_PAID_ENV):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)


def entry(fingerprint=FP, usd=0.5, route="rest", job="out/a"):
    return {"jobDir": job, "fingerprint": fingerprint, "provider": "xai", "model": "grok-imagine-video-1.5",
            "kind": "video", "route": route, "reservedUsd": usd}


def png():
    output = io.BytesIO()
    Image.new("RGBA", (16, 16), (200, 40, 40, 255)).save(output, "PNG")
    return output.getvalue()


class Fake:
    def __init__(self, results=()):
        self.results, self.calls = iter(results), []

    def api(self, method, url, key, body=None, content_type=None, timeout=90, meta=None):
        self.calls.append(method)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def download(self, url, timeout=90):
        return b"\x00\x00\x00\x18ftypmp42data"


@pytest.fixture
def video_cli(tmp_path):
    """argv builder for an xAI video request priced at 4 s x 0.14 + 1 x 0.01 = 0.57 USD."""
    (tmp_path / "prompt.txt").write_text("A slime idles in place.", encoding="utf-8")
    (tmp_path / "base.png").write_bytes(png())

    def build(out="job", *extra):
        return ["video", "--provider", "xai", "--model", "grok-imagine-video-1.5", "--prompt-file", str(tmp_path / "prompt.txt"),
                "--reference", str(tmp_path / "base.png"), "--duration", "4", "--resolution", "720p",
                "--out-dir", str(tmp_path / out), "--project-dir", str(tmp_path / "project"), *extra]
    return build


def test_reserve_commit_and_unknown_keeps_reservation(tmp_path):
    ledger = ml.Ledger(tmp_path)
    assert not ledger.lines() and not (tmp_path / ".forge").exists()  # reading creates nothing
    done = ledger.reserve(entry(usd=0.5))
    assert ledger.totals()["heldUsd"] == 0.5 and ledger.entries()[done]["status"] == "reserved"
    ledger.commit(done, status="done", actual_usd=0.45)
    unknown = ledger.reserve(entry("cd" * 32, usd=0.25))
    ledger.commit(unknown, status="unknown")
    released = [ledger.reserve(entry("ef" * 32, usd=9.0)) for _ in range(2)]
    ledger.commit(released[0], status="failed")
    ledger.commit(released[1], status="not_sent")
    totals = ledger.totals()
    assert totals["committedUsd"] == 0.45 and totals["heldUsd"] == 0.25 and totals["usd"] == 0.7
    assert totals["calls"] == 3 and totals["unknown"] == 1  # not_sent is not a call
    assert [line["status"] for line in ledger.lines()] == ["reserved", "done", "reserved", "unknown",
                                                           "reserved", "reserved", "failed", "not_sent"]
    assert ledger.find_success(FP)["actualUsd"] == 0.45 and ledger.find_success("cd" * 32) is None
    # Every line is a complete ledger_line_v1 record (Appendix B) plus reservationId.
    for line in ledger.lines():
        assert set(line) >= {"ts", "jobDir", "fingerprint", "provider", "model", "kind", "route",
                             "reservedUsd", "status", "quotaCall", "reservationId"}
    with pytest.raises(ml.LedgerError, match="Unknown reservation"):
        ledger.commit("missing", status="done")
    with pytest.raises(ValueError):
        ledger.commit(done, status="reserved")


def test_cap_blocks_before_send(video_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    ledger = ml.Ledger(tmp_path / "project")
    prior = ledger.reserve(entry("12" * 32, usd=0.5))
    ledger.commit(prior, status="unknown")  # an unknown outcome still holds its 0.5 USD
    blocked = [(["--budget-usd", "1.0"], "budget"), (["--max-calls", "1"], "call cap")]
    for extra, message in blocked:
        transport = Fake()
        with pytest.raises(media.MediaError, match=message) as caught:
            media.execute(media.parser().parse_args(video_cli("job", "--execute", *extra)), transport)
        assert caught.value.code == "cap" and not transport.calls and not (tmp_path / "job").exists()
    monkeypatch.setenv(ml.MAX_PAID_ENV, "1")
    with pytest.raises(media.MediaError, match=ml.MAX_PAID_ENV):
        media.execute(media.parser().parse_args(video_cli("job", "--execute")), Fake())
    monkeypatch.setenv(ml.MAX_PAID_ENV, "lots")
    with pytest.raises(media.MediaError, match="whole number"):
        media.execute(media.parser().parse_args(video_cli("job", "--execute")), Fake())
    monkeypatch.setenv(ml.MAX_PAID_ENV, "2")
    transport = Fake([{"request_id": "req-1"}, {"status": "done", "video": {"url": "https://media.example/a.mp4"}}])
    job = media.execute(media.parser().parse_args(video_cli("job", "--execute", "--budget-usd", "1.07", "--max-calls", "2")), transport)
    assert job["status"] == "done" and transport.calls == ["POST", "GET"]
    assert ledger.totals()["usd"] == pytest.approx(1.07) and ledger.totals()["paidCalls"] == 2
    assert [line["status"] for line in ledger.lines()] == ["reserved", "unknown", "reserved", "done"]


def test_unpriced_request_cannot_use_a_usd_budget(video_cli, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", KEY)
    with pytest.raises(media.MediaError, match="no verified price"):  # 480p has no price row
        media.execute(media.parser().parse_args(video_cli("job", "--execute", "--resolution", "480p",
                                                          "--budget-usd", "100")), Fake())


def test_dry_run_prints_estimate(video_cli, tmp_path, capsys):
    assert media.main(video_cli()) == 0
    printed = capsys.readouterr().out
    plan = json.loads(printed)
    assert len(printed.splitlines()) == 1 and printed.isascii()
    assert plan["execution"] == "dry-run" and plan["estimate"]["usd"] == 0.57
    assert plan["estimate"]["pricesVersion"] == "2026-10-05" and "verified 2026-10-05" in plan["estimate"]["basis"]
    assert plan["consent"] == {"provider": "xai", "model": "grok-imagine-video-1.5", "calls": 1,
                               "estimateUsd": 0.57, "apiHost": "api.x.ai"}
    assert plan["ledger"]["calls"] == 0 and plan["warnings"] == []
    assert not (tmp_path / "job").exists() and not (tmp_path / "project").exists()
    # A dry run reports, but does not enforce, a cap that --execute would hit.
    assert media.main(video_cli("job", "--budget-usd", "0.5")) == 0
    assert "would be refused" in json.loads(capsys.readouterr().out)["warnings"][0]


def test_prices_rows_carry_source_and_verified_at():
    prices = ml.load_prices()
    assert prices["rows"] and ml.prices_version(prices) == "2026-10-05"
    for row in prices["rows"]:
        assert row["source"].startswith("https://") and ml.DATE.fullmatch(row["verifiedAt"])
    bad = {"rows": [{"provider": "x", "model": "m", "unit": "output_image", "usd": 1}]}
    path = Path.cwd() / "bad-prices.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ml.LedgerError, match="verifiedAt"):
        ml.load_prices(path)


def plan(provider="xai", model="grok-imagine-video-1.5", kind="video", options=None, refs=1, last=False):
    return {"provider": provider, "kind": kind, "requestedModel": model, "options": options or {},
            "references": [{}] * refs, "lastFrame": {} if last else None, "paidRequests": 1}


def test_estimate_prices_known_rows_and_names_missing_ones():
    lite = ml.estimate(plan(model="grok-imagine-video-1.5-lite", options={"duration": 4, "resolution": "720p"}))
    assert lite["usd"] == 0.13 and [i["unit"] for i in lite["items"]] == ["output_second", "input_image"]
    pinned = ml.estimate(plan(options={"duration": 6, "resolution": "720p"}, last=True))
    assert pinned["usd"] == round(6 * 0.14 + 2 * 0.01, 6)
    image = ml.estimate(plan(model="grok-imagine-image-2.0", kind="image", options={"n": 1, "resolution": "2k", "quality": "medium"}))
    assert image["usd"] == 0.09
    for unpriced in (plan(options={"duration": 4, "resolution": "480p"}),
                     plan(model="grok-imagine-image-2.0", kind="image", options={"n": 1}),
                     plan(provider="openai", model="gpt-image-2.5-sunburst", kind="image", options={"n": 1, "size": "1024x1024"})):
        result = ml.estimate(unpriced)
        assert result["usd"] is None and result["basis"].startswith("unpriced")
    assert "known rows" in ml.estimate(plan(options={"duration": 4, "resolution": "480p"}))["basis"]
    quota = ml.estimate({"route": "grok-cli", "provider": "xai", "kind": "video"})
    assert quota["usd"] == 0.0 and "quota" in quota["basis"]


def test_fingerprint_ignores_output_paths_and_tracks_inputs():
    base = {"provider": "xai", "kind": "video", "route": "rest", "endpoint": "https://api.x.ai/v1/videos/generations",
            "requestedModel": "grok-imagine-video-1.5", "options": {"duration": 4}, "promptSha256": "0" * 64}
    same = ml.fingerprint({**base, "outDir": "/a", "execution": "execute"}, ["1" * 64])
    assert same == ml.fingerprint({**base, "outDir": "/b"}, ["1" * 64])
    assert ml.SHA256.fullmatch(same)
    assert same != ml.fingerprint({**base, "options": {"duration": 5}}, ["1" * 64])
    assert same != ml.fingerprint(base, ["2" * 64])
    assert same != ml.fingerprint({**base, "promptSha256": "f" * 64}, ["1" * 64])


def test_duplicate_guard_is_atomic_with_the_reservation(tmp_path):
    ledger = ml.Ledger(tmp_path)
    first = ledger.reserve(entry(), refuse_duplicate=True)
    with pytest.raises(ml.DuplicateRequest, match="in flight"):
        ledger.reserve(entry(), refuse_duplicate=True)
    ledger.commit(first, status="done")
    with pytest.raises(ml.DuplicateRequest, match="already succeeded"):
        ledger.reserve(entry(), refuse_duplicate=True)
    assert ledger.reserve(entry())  # callers that allow duplicates may reserve again


def test_concurrent_reservations_cannot_overshoot_a_cap(tmp_path):
    ledger_dir = tmp_path / "shared"
    outcomes, barrier = [], threading.Barrier(8)

    def worker(index):
        barrier.wait()
        try:
            ml.Ledger(ledger_dir).reserve(entry(f"{index:02d}" * 32), max_calls=3)
            outcomes.append("reserved")
        except ml.CapExceeded:
            outcomes.append("capped")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["capped"] * 5 + ["reserved"] * 3
    assert len(ml.Ledger(ledger_dir).lines()) == 3


def test_quota_calls_count_as_calls_but_not_paid_requests(tmp_path, monkeypatch):
    ledger = ml.Ledger(tmp_path)
    quota = ledger.reserve(entry(usd=0.0, route="grok-cli"))
    ledger.commit(quota, status="done")
    totals = ledger.totals()
    assert (totals["calls"], totals["quotaCalls"], totals["paidCalls"]) == (1, 1, 0)
    monkeypatch.setenv(ml.MAX_PAID_ENV, "1")
    ledger.check_caps()  # one paid request still fits
    with pytest.raises(ml.CapExceeded, match="call cap"):
        ledger.check_caps(max_calls=1, quota_call=True)
    monkeypatch.setenv(ml.MAX_PAID_ENV, "0")
    with pytest.raises(ml.CapExceeded):
        ledger.check_caps()
    ledger.check_caps(quota_call=True)  # the paid-request cap does not block quota routes


def test_torn_last_line_is_skipped_and_never_merged(tmp_path):
    ledger = ml.Ledger(tmp_path)
    rid = ledger.reserve(entry())
    with open(ledger.path, "ab") as handle:
        handle.write(b'{"ts": "2026-10-05T00:00:00.000Z", "reservationId": "torn", "sta')
    assert list(ledger.entries()) == [rid]
    ledger.commit(rid, status="done")
    assert [line["status"] for line in ledger.lines()] == ["reserved", "done"]


def test_hand_edited_or_foreign_lines_do_not_break_the_ledger(tmp_path, monkeypatch):
    ledger = ml.Ledger(tmp_path)
    rid = ledger.reserve(entry(usd=1.0))
    with open(ledger.path, "a", encoding="utf-8") as handle:
        handle.write('{"reservationId": "no-ts", "status": "done", "reservedUsd": "lots"}\n')
        handle.write('["not", "an", "object"]\n')
    totals = ledger.totals()
    assert totals["calls"] == 2 and totals["heldUsd"] == 1.0 and totals["unpricedCalls"] == 1
    ledger.commit(rid, status="done")
    monkeypatch.setenv(ml.MAX_PAID_ENV, "²")  # str.isdigit() accepts it, int() does not
    with pytest.raises(ml.LedgerError, match="whole number"):
        ledger.check_caps()


def test_reserve_validates_entries(tmp_path):
    ledger = ml.Ledger(tmp_path)
    for broken, message in (({**entry(), "prompt": "secret"}, "Unknown"), ({**entry(), "route": "web"}, "route"),
                            ({**entry(), "fingerprint": "x"}, "sha256"), ({**entry(), "reservedUsd": -1}, "reservedUsd")):
        with pytest.raises(ValueError, match=message):
            ledger.reserve(broken)
    assert not ledger.path.exists()


def run_ledger_cli(*args, encoding="utf-8"):
    env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(SCRIPTS / "media_ledger.py"), *args], capture_output=True, env=env, timeout=60)


@pytest.mark.parametrize("command", [[], ["summary"], ["settle"]])
def test_ledger_help_works_under_cp1252(command):
    done = run_ledger_cli(*command, "--help", encoding="cp1252")
    assert done.returncode == 0 and done.stdout.decode("ascii").startswith("usage:")


def test_settle_releases_an_unknown_reservation(tmp_path):
    ledger = ml.Ledger(tmp_path)
    rid = ledger.reserve(entry(usd=2.0))
    ledger.commit(rid, status="unknown")
    with pytest.raises(ml.CapExceeded):
        ledger.check_caps(budget_usd=3.0, next_usd=1.5)
    summary = run_ledger_cli("summary", "--project-dir", str(tmp_path))
    assert summary.returncode == 0
    assert [u["reservationId"] for u in json.loads(summary.stdout)["unsettled"]] == [rid]
    settled = run_ledger_cli("settle", rid, "--status", "failed", "--project-dir", str(tmp_path))
    assert settled.returncode == 0 and json.loads(settled.stdout)["status"] == "failed"
    assert ledger.check_caps(budget_usd=3.0, next_usd=1.5)["usd"] == 0.0
    missing = run_ledger_cli("settle", "nope", "--status", "done", "--project-dir", str(tmp_path))
    assert missing.returncode == 1 and missing.stderr.decode("ascii").startswith("error: Unknown reservation")


def test_ledger_lines_hold_no_provider_text(video_cli, tmp_path, monkeypatch):
    """Ledger lines carry only enums, numbers, hashes and paths, never provider output."""
    monkeypatch.setenv("XAI_API_KEY", KEY)
    transport = Fake([{"request_id": "req-2"}, {"status": "done", "model": KEY, "video": {"url": "https://media.example/b.mp4"}}])
    media.execute(media.parser().parse_args(video_cli("job", "--execute")), transport)
    text = ml.Ledger(tmp_path / "project").path.read_text(encoding="utf-8")
    assert KEY not in text and "media.example" not in text and "slime" not in text
    lines = [json.loads(raw) for raw in text.splitlines()]
    assert [line["status"] for line in lines] == ["reserved", "done"]
    assert lines[-1]["jobDir"] == (tmp_path / "job").resolve().as_posix()  # outside the project: absolute
    assert lines[-1]["reservedUsd"] == 0.57 and lines[-1]["quotaCall"] is False
