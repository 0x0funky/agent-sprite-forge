#!/usr/bin/env python3
"""Keyer benchmark: report v2 tables 4-A (defects) and 4-B (stability, fidelity, speed) on a real clip.

Opt-in and local: it reads FORGE_BENCH_CLIP (a video, or a directory of decoded
frames named frame_0001.png ...) and, for the auto interior-despill rule, the
optional reference still FORGE_BENCH_REFERENCE. Frames are 0-based; the
default window 57:88 is report v2's 57-87.

    python tests/benchmarks/keyer_bench.py --clip <grok-native.mp4 or frames-raw dir> --reference <input.png> --frames 57:88
    python -m pytest tests/benchmarks/keyer_bench.py -m bench

Methods: ``legacy`` (cfed170 video keyer, despill 0.5: the report's Forge
column), ``soft`` (soft_matte, auto interior despill: the frozen v13
prototype), ``soft+hysteresis`` (plus temporal_alpha_hysteresis),
``soft-local`` (local_background) and ``dominance``.

Metrics are a port of the audit's edge_metrics.py (no ground truth; proxies):
K is the median of a 16 px border ring of the raw frame; defBG is |C-K| <= 24,
defFG |C-K| >= 180; the band is within 4 px of both defBG and non-defBG, from
the raw frame only, shared by every method. E = alpha * max(0, min(R,B) - G)
of the output colour. Flips: band pixels whose raw colour changed by <= 20
(max channel) while alpha changed by > 0.25, per frame pair.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
METHODS = ("legacy", "soft", "soft+hysteresis", "soft-local", "dominance")
DEF_BG, DEF_FG, BAND_R, REF_R = 24.0, 180.0, 4, 6


def _load_forge_matte():
    spec = importlib.util.spec_from_file_location("forge_matte_bench", ROOT / "shared" / "forge_matte.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- frames

def _parse_window(text: str) -> range:
    start, _, stop = text.partition(":")
    window = range(int(start), int(stop))
    if len(window) < 2:
        raise ValueError(f"--frames needs at least two frames (start:stop, 0-based); got {text!r}.")
    return window


def _video_size(clip: Path) -> tuple[int, int]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", str(clip)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def load_frames(clip: Path, wanted: set[int]) -> tuple[dict[int, np.ndarray], int]:
    """RGB frames by 0-based index, and the clip's frame count. Directories hold frame_0001.png ..."""
    if clip.is_dir():
        paths = sorted(clip.glob("frame_*.png")) or sorted(clip.glob("*.png"))
        frames = {index: np.asarray(Image.open(paths[index]).convert("RGB")) for index in sorted(wanted)
                  if index < len(paths)}
        return frames, len(paths)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Decoding a video needs ffmpeg and ffprobe on PATH; or pass a frames directory.")
    width, height = _video_size(clip)
    size = width * height * 3
    process = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(clip), "-map", "0:v:0", "-f", "rawvideo",
                                "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE)
    frames, count = {}, 0
    try:
        while (chunk := process.stdout.read(size)) and len(chunk) == size:
            if count in wanted:
                frames[count] = np.frombuffer(chunk, np.uint8).reshape(height, width, 3).copy()
            count += 1
    finally:
        process.stdout.close()
        process.wait()
    return frames, count


# --------------------------------------------------------------------------- report v2 edge metrics (port)

def _grow(mask: np.ndarray, radius: int) -> np.ndarray:
    result = mask.copy()
    for _ in range(radius):
        grown = result.copy()
        grown[:, 1:] |= result[:, :-1]
        grown[:, :-1] |= result[:, 1:]
        step = grown.copy()
        step[1:] |= grown[:-1]
        step[:-1] |= grown[1:]
        result = step
    return result


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _cbcr(rgb: np.ndarray) -> np.ndarray:
    y = _luma(rgb)
    return np.stack([(rgb[..., 2] - y) * 0.564, (rgb[..., 0] - y) * 0.713], axis=-1)


class RawFrame:
    def __init__(self, rgb: np.ndarray):
        self.rgb = rgb.astype(np.float32)
        ring = np.zeros(rgb.shape[:2], bool)
        ring[:16] = ring[-16:] = True
        ring[:, :16] = ring[:, -16:] = True
        self.K = np.median(self.rgb[ring].reshape(-1, 3), axis=0)
        dk = np.sqrt(((self.rgb - self.K) ** 2).sum(-1))
        self.def_bg, self.def_fg = dk <= DEF_BG, dk >= DEF_FG
        self.band = _grow(self.def_bg, BAND_R) & _grow(~self.def_bg, BAND_R)
        self.deep = self.def_fg & ~_grow(~self.def_fg, 3)
        self.Y = _luma(self.rgb)
        self.YK = float(_luma(self.K[None, None, :])[0, 0])
        self.luma_fg = np.abs(self.Y - self.YK) >= 0.5 * max(self.YK, 255.0 - self.YK)
        self.ref_c = _cbcr(self.rgb)
        self.m_raw = np.minimum(self.rgb[..., 0], self.rgb[..., 2]) - self.rgb[..., 1]
        self.m_K = float(min(self.K[0], self.K[2]) - self.K[1])


def _offchroma(raw: RawFrame, colour: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    ys, xs = np.nonzero(raw.band & (alpha >= 0.1))
    height, width = alpha.shape
    chroma = _cbcr(colour[ys, xs])
    best = np.full(len(ys), np.inf, np.float32)
    for dy in range(-REF_R, REF_R + 1):
        for dx in range(-REF_R, REF_R + 1):
            yy, xx = np.clip(ys + dy, 0, height - 1), np.clip(xs + dx, 0, width - 1)
            usable = raw.deep[yy, xx]
            if usable.any():
                distance = np.sqrt(((chroma - raw.ref_c[yy, xx]) ** 2).sum(-1))
                np.minimum(best, np.where(usable, distance, np.inf), out=best)
    return np.where(np.isfinite(best), best * alpha[ys, xs], 0.0)


def frame_metrics(raw: RawFrame, rgba: np.ndarray, forge_core) -> dict:
    colour = rgba[..., :3].astype(np.float32)
    alpha = rgba[..., 3].astype(np.float32) / 255.0
    excess = np.maximum(0.0, np.minimum(colour[..., 0], colour[..., 2]) - colour[..., 1])
    fringe = alpha * excess
    band = raw.band
    labels, count = forge_core.label_components(alpha > 0, 8)
    areas = np.bincount(labels.ravel())[1:] if count else np.zeros(0, int)
    specks = int(((areas < 64).sum() - (areas.max() < 64)) if count > 1 else 0)
    recon = np.abs(alpha * _luma(colour) + (1 - alpha) * raw.YK - raw.Y)
    return {
        "spill_px8": int((fringe[band] > 8).sum()),
        "spill_px24": int((fringe[band] > 24).sum()),
        "spill_px24_all": int((fringe > 24).sum()),
        "leak_px": int((raw.def_bg & (alpha > 0.15)).sum()),
        "keyhue_opaque_px": int(((alpha >= 0.5) & (raw.m_raw >= 0.5 * raw.m_K)).sum()),
        "keyblob_px": int(((alpha >= 0.5) & (excess >= 100)).sum()),
        "offchroma_px16": int((_offchroma(raw, colour, alpha) > 16).sum()),
        "specks": specks,
        "erosion_luma_px": int((raw.luma_fg & (alpha < 0.5)).sum()),
        "luma_recon_err": float(recon[band].mean()),
        "partial_frac": float(((alpha[band] > 0) & (alpha[band] < 1)).mean()),
    }


def pair_flips(first: RawFrame, second: RawFrame, alpha_a: np.ndarray, alpha_b: np.ndarray) -> int:
    changed = np.abs(first.rgb - second.rgb).max(-1) <= 20
    return int(((first.band | second.band) & changed & (np.abs(alpha_a - alpha_b) > 0.25)).sum())


# --------------------------------------------------------------------------- bench

def run_bench(clip: Path, window: range, reference: Path | None = None,
              methods: tuple[str, ...] = METHODS) -> dict:
    fm = _load_forge_matte()
    wanted = set(window)
    probe_frames, count = load_frames(clip, {0})
    count = count or len(probe_frames)
    samples = sorted({int(np.floor(k * (count - 1) / 15 + 0.5)) for k in range(16)})
    frames, _ = load_frames(clip, wanted | set(samples))
    missing = sorted(wanted - set(frames))
    if missing:
        raise ValueError(f"The clip has {count} frames; window frames {missing[:5]} are missing.")
    reference_rgb = np.asarray(Image.open(reference).convert("RGB")) if reference else None
    decision = fm.auto_interior_despill(reference_rgb, [frames[index] for index in samples])
    decision["clip_sample_frames"] = samples  # clip frame numbers, not positions in the sample list
    params = fm.KeyParams(interior_despill=decision["interior_despill"])
    keyers = {
        "legacy": lambda rgb: np.asarray(fm.legacy_border_flood_key(rgb, 55.0, 0.5)),
        "soft": lambda rgb: fm.soft_matte(rgb, params),
        "soft-local": lambda rgb: fm.soft_matte(rgb, params, local_background=True),
        "dominance": lambda rgb: fm.dominance_matte(rgb, fm.estimate_key(rgb)[0]),
    }
    raws = {index: RawFrame(frames[index]) for index in window}
    outputs, seconds = {}, {}
    for method in methods:
        base = "soft" if method == "soft+hysteresis" else method
        if base not in outputs:
            keyed, times = [], []
            for index in window:
                start = time.perf_counter()
                keyed.append(keyers[base](frames[index]))
                times.append(time.perf_counter() - start)
            outputs[base], seconds[base] = keyed, float(np.median(times))
        if method == "soft+hysteresis":
            start = time.perf_counter()
            outputs[method] = fm.temporal_alpha_hysteresis(outputs["soft"])
            seconds[method] = seconds["soft"] + (time.perf_counter() - start) / len(window)
    summary = {}
    for method in methods:
        rows = [frame_metrics(raws[index], rgba, fm.forge_core) for index, rgba in zip(window, outputs[method])]
        alphas = [rgba[..., 3].astype(np.float32) / 255.0 for rgba in outputs[method]]
        flips = [pair_flips(raws[a], raws[b], alphas[k], alphas[k + 1])
                 for k, (a, b) in enumerate(zip(window, window[1:]))]
        row = {key: float(np.mean([r[key] for r in rows])) for key in rows[0]}
        row.update({"flips_per_pair": float(np.mean(flips)), "seconds_per_frame": seconds[method],
                    "leak_frames_nonzero": int(sum(r["leak_px"] > 0 for r in rows))})
        summary[method] = row
    return {"clip": str(clip), "frames": [window.start, window.stop], "decision": decision,
            "params": params.to_dict(), "summary": summary,
            "method": "report v2 edge_metrics.py port: raw-derived band, no ground truth (proxies)"}


TABLE_4A = [("visible fringe px (band E>8)", "spill_px8"), ("obvious fringe px (band E>24)", "spill_px24"),
            ("frame px E>24", "spill_px24_all"), ("leak px", "leak_px"),
            ("key-hued raw kept opaque px", "keyhue_opaque_px"), ("off-chroma px >16", "offchroma_px16"),
            ("specks (< 64 px)", "specks")]
TABLE_4B = [("flips per frame pair", "flips_per_pair"), ("luma recon error", "luma_recon_err"),
            ("band semi-transparent share", "partial_frac"), ("erosion (luma bound) px", "erosion_luma_px"),
            ("seconds per frame", "seconds_per_frame")]


def format_tables(result: dict) -> str:
    methods = list(result["summary"])
    lines = []
    for title, table in (("Table 4-A defects", TABLE_4A), ("Table 4-B stability, fidelity, speed", TABLE_4B)):
        lines += [f"{title} (frames {result['frames'][0]}:{result['frames'][1]}, mean per frame)", "",
                  "| metric | " + " | ".join(methods) + " |", "|---|" + "---:|" * len(methods)]
        for label, key in table:
            cells = [f"{result['summary'][method][key]:.3f}" if key in ("luma_recon_err", "partial_frac",
                                                                        "seconds_per_frame")
                     else f"{result['summary'][method][key]:.1f}" for method in methods]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append(f"interior despill: {json.dumps(result['decision'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce report v2 keying tables 4-A and 4-B on a local clip.")
    parser.add_argument("--clip", type=Path, default=os.environ.get("FORGE_BENCH_CLIP"),
                        help="Video or frames directory (default: FORGE_BENCH_CLIP).")
    parser.add_argument("--reference", type=Path, default=os.environ.get("FORGE_BENCH_REFERENCE"),
                        help="Reference still for the auto interior-despill rule (default: FORGE_BENCH_REFERENCE).")
    parser.add_argument("--frames", default="57:88", help="0-based start:stop window (default 57:88).")
    parser.add_argument("--methods", default=",".join(METHODS), help=f"Comma list from {', '.join(METHODS)}.")
    parser.add_argument("--json-out", type=Path, help="Also write the full result as JSON (new file).")
    args = parser.parse_args(argv)
    try:
        if args.clip is None:
            raise ValueError("Pass --clip or set FORGE_BENCH_CLIP.")
        methods = tuple(name.strip() for name in args.methods.split(",") if name.strip())
        unknown = sorted(set(methods) - set(METHODS))
        if unknown:
            raise ValueError(f"Unknown methods: {', '.join(unknown)}.")
        result = run_bench(args.clip, _parse_window(args.frames), args.reference, methods)
        if args.json_out:
            with open(args.json_out, "x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(format_tables(result))
    return 0


@pytest.mark.bench
def test_keyer_bench_soft_matte_window():
    """A2-T8: on FORGE_BENCH_CLIP frames 57:88 the soft matte reports fringe 0 and leak 0."""
    clip = os.environ.get("FORGE_BENCH_CLIP")
    if not clip:
        pytest.skip("set FORGE_BENCH_CLIP to the Ryo clip or its frames-raw directory")
    reference = os.environ.get("FORGE_BENCH_REFERENCE")
    result = run_bench(Path(clip), range(57, 88), Path(reference) if reference else None,
                       ("soft", "soft+hysteresis"))
    for method in ("soft", "soft+hysteresis"):
        row = result["summary"][method]
        assert row["spill_px8"] == 0 and row["leak_px"] == 0, (method, row)
    assert result["summary"]["soft+hysteresis"]["flips_per_pair"] <= 5.7


if __name__ == "__main__":
    raise SystemExit(main())
