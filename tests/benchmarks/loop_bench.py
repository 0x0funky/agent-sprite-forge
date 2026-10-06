#!/usr/bin/env python3
"""Loop-selection benchmark: report v2 section 4.5 / P0-4 acceptance on a real keyed clip.

Opt-in and local. It reads FORGE_BENCH_LOOP_FRAMES (a directory of keyed RGBA frames such
as the Ryo clip's forge-process/frames-clean, 145 frames at 960x960, 24 fps), or
FORGE_BENCH_CLIP when that points at such a directory. Nothing is written outside a
temporary directory.

    python tests/benchmarks/loop_bench.py --frames <frames-clean dir> --fps 24
    python -m pytest tests/benchmarks/loop_bench.py -m bench

Acceptance (B07-T1): `gait_loop select` returns a 16-frame loop starting at frame 81-84,
marks frames 0-25 unusable, rejects with reasons the six invalid candidates that the
old review helper listed (report v2: 12-22, 25-42, 75-82, 60-73, 95-117, 30-54, written
here 0-based end-exclusive), keeps the two valid ones (79-94, 53-83) and finishes within
40 s including the review aids.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALID = [(79, 95), (53, 84)]
INVALID = [(12, 23), (25, 43), (75, 83), (60, 74), (95, 118), (30, 55)]
BUDGET_SECONDS = 40.0


def _load_gait_loop():
    sys.path.insert(0, str(ROOT / "skills" / "video2dsprite" / "scripts"))
    import gait_loop
    return gait_loop


def _frames_dir() -> Path | None:
    for name in ("FORGE_BENCH_LOOP_FRAMES", "FORGE_BENCH_CLIP"):
        value = os.environ.get(name)
        if value and Path(value).is_dir():
            return Path(value)
    return None


def run_bench(frames: Path, fps: str = "24", state: str = "run") -> dict:
    """Run `gait_loop select` (with aids) into a temporary directory and collect the evidence."""
    gait_loop = _load_gait_loop()
    evaluate = [f"{s}:{e}" for s, e in VALID + INVALID]
    with tempfile.TemporaryDirectory(prefix="loop-bench-") as tmp:
        out = Path(tmp) / "select"
        argv = ["select", "--frames-dir", str(frames), "--fps", fps, "--state", state, "--output-dir", str(out)]
        for window in evaluate:
            argv += ["--evaluate", window]
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            code = gait_loop.main(argv)
        seconds = time.perf_counter() - started
        if code != 0:
            raise RuntimeError("gait_loop select failed on the bench clip")
        report = json.loads((out / "loop-report.json").read_text(encoding="utf-8"))
    window = report["recommendation"]["window"]
    return {"frames": str(frames), "seconds": seconds, "T_frames": report["period"]["T_frames"],
            "verdict": report["period"]["verdict"], "unusable": report["unusableFrames"],
            "recommended": [window["start"], window["endExclusive"]],
            "confidence": report["recommendation"]["confidence"]["label"],
            "evaluated": {f"{w['start']}:{w['endExclusive']}": w["classification"] for w in report["evaluated"]}}


def format_result(result: dict) -> str:
    lines = [f"clip {result['frames']}",
             f"period {result['T_frames']:.2f} frames ({result['verdict']}), recommended "
             f"{result['recommended'][0]}-{result['recommended'][1] - 1} ({result['confidence']}), "
             f"{result['seconds']:.1f} s",
             f"unusable frames: {len(result['unusable'])} ({min(result['unusable'], default='-')}-"
             f"{max(result['unusable'], default='-')})", "",
             "| window (0-based, end exclusive) | class | first reason |", "|---|---|---|"]
    for window, verdict in result["evaluated"].items():
        lines.append(f"| {window} | {verdict['class']} | {(verdict['reasons'] or [''])[0]} |")
    return "\n".join(lines)


def check(result: dict) -> list[str]:
    """Acceptance failures (empty when the bench passes)."""
    problems = []
    start, end = result["recommended"]
    if end - start != 16 or not 81 <= start <= 84:
        problems.append(f"recommended {start}:{end}, expected 16 frames starting at 81-84")
    unusable = set(result["unusable"])
    if not set(range(26)) <= unusable or 26 in unusable:
        problems.append(f"unusable frames should be exactly 0-25 at the start, got {sorted(unusable)[:30]}")
    for s, e in INVALID:
        verdict = result["evaluated"][f"{s}:{e}"]
        if verdict["class"] != "rejected" or not verdict["reasons"]:
            problems.append(f"{s}:{e} should be rejected with reasons, got {verdict['class']}")
    for s, e in VALID:
        if result["evaluated"][f"{s}:{e}"]["class"] == "rejected":
            problems.append(f"{s}:{e} is a valid loop but was rejected")
    if result["seconds"] > BUDGET_SECONDS:
        problems.append(f"took {result['seconds']:.1f} s, budget {BUDGET_SECONDS:.0f} s")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report v2 loop-selection acceptance on a local keyed clip.")
    parser.add_argument("--frames", type=Path, default=_frames_dir(),
                        help="keyed RGBA frames directory (default: FORGE_BENCH_LOOP_FRAMES or FORGE_BENCH_CLIP)")
    parser.add_argument("--fps", default="24")
    parser.add_argument("--state", default="run", choices=("run", "walk"))
    parser.add_argument("--json-out", type=Path, help="also write the result as JSON (new file)")
    args = parser.parse_args(argv)
    try:
        if args.frames is None:
            raise ValueError("pass --frames or set FORGE_BENCH_LOOP_FRAMES")
        result = run_bench(args.frames, args.fps, args.state)
        if args.json_out:
            with open(args.json_out, "x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(format_result(result))
    problems = check(result)
    for problem in problems:
        print(f"FAIL: {problem}")
    return 1 if problems else 0


@pytest.mark.bench
def test_loop_bench_report_v2_acceptance():
    """B07-T1 bench: 16 frames from 81-84, frames 0-25 unusable, six invalid candidates rejected, <= 40 s."""
    frames = _frames_dir()
    if frames is None:
        pytest.skip("set FORGE_BENCH_LOOP_FRAMES to the Ryo clip's keyed frames-clean directory")
    result = run_bench(frames)
    assert check(result) == [], format_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
