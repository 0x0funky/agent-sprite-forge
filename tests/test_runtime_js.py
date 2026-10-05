"""video2dsprite runtime references (B09-T2, B09-T3) under Node, and their parity with the Python side.

Runs tests/js/forge-runtime.test.mjs with node --test, syntax-checks the three
runtime files, and cross-checks splitDurations against
forge_core.frame_durations, the CPU packed-alpha kernel against
forge_av.unpack_packed_alpha, and the runtime reading an engine-export.json
written by export_engine.py. These tests carry the node marker and skip
without node.

test_webgl_compositor_in_a_headless_browser runs only when FORGE_BROWSER names
a Chrome or Edge executable: it composes a packed frame on the GPU (SwiftShader
when there is no GPU) and compares the result with the CPU kernel. It is never
part of the standard run.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forge_testutils import REPO_ROOT, load_script, require_node, run_cli, script_path

REFERENCES = REPO_ROOT / "skills" / "video2dsprite" / "references"
RUNTIME = REFERENCES / "runtime" / "forge-runtime.mjs"
WEBGL = REFERENCES / "runtime" / "packed-alpha-webgl.mjs"
WRAPPER = REFERENCES / "packed-alpha-runtime.js"
JS_TESTS = REPO_ROOT / "tests" / "js" / "forge-runtime.test.mjs"


@pytest.fixture(scope="module")
def node() -> str:
    return require_node()


def run_node(node: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([node, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


def node_json(node: str, script: str) -> object:
    """Run an ES module snippet and parse the JSON it prints."""
    result = run_node(node, "--input-type=module", "-e", script)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.node
def test_runtime_sources_pass_node_check(node, tmp_path):
    for source in (RUNTIME, WEBGL, WRAPPER):
        result = run_node(node, "--check", str(source))
        if result.returncode and source.suffix == ".js":
            # Node before 22.7 parses a typeless .js file as CommonJS; check the same text as a module.
            copy = tmp_path / f"{source.stem}.mjs"
            shutil.copyfile(source, copy)
            result = run_node(node, "--check", str(copy))
        assert result.returncode == 0, f"{source.name}: {result.stderr}"
        assert source.read_text(encoding="utf-8").isascii(), source.name


@pytest.mark.node
def test_node_unit_tests_pass(node):
    """impact lands on each hit, phase is frozen while blocked, frameAt sums exactly, seam UV and alpha snap, ..."""
    result = run_node(node, "--test", str(JS_TESTS), timeout=300)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert re.search(r"# fail 0", result.stdout), result.stdout[-2000:]


@pytest.mark.node
def test_split_durations_matches_forge_core(node):
    core = load_script("generate2dsprite", "forge_core")
    cases = [[1000, 3], [333, 4], [1001, 30], [540, 7], [5, 5], [99991, 17]]
    script = (f"import {{splitDurations}} from {json.dumps(RUNTIME.as_uri())};\n"
              f"console.log(JSON.stringify({json.dumps(cases)}.map(([t, n]) => splitDurations(t, n))));")
    assert node_json(node, script) == [core.frame_durations(total, n) for total, n in cases]


@pytest.mark.node
def test_cpu_kernel_matches_forge_av_unpack(node, tmp_path):
    """The JS CPU compositor crops exactly like forge_av.unpack_packed_alpha (alpha = red of the right half)."""
    av = load_script("video2dsprite", "forge_av")
    geometry = av.packed_geometry(403, 37)                      # odd logical size: halves padded to 404 x 38
    rng = np.random.default_rng(9)
    frame = rng.integers(0, 256, (geometry["halfHeight"], geometry["encodedSize"][0], 3), dtype=np.uint8)
    frame[:, geometry["halfWidth"]:, 1:] = rng.integers(0, 256, (geometry["halfHeight"], geometry["halfWidth"], 2))
    frame[:4, geometry["halfWidth"]:geometry["halfWidth"] + 8, 0] = [0, 1, 2, 3, 252, 253, 254, 255]
    rgba = np.dstack([frame, np.full(frame.shape[:2], 255, np.uint8)])
    (tmp_path / "frame.raw").write_bytes(rgba.tobytes())
    meta = {key: geometry[key] for key in ("layout", "width", "height", "halfWidth", "halfHeight")}
    script = f"""
import {{readFileSync, writeFileSync}} from 'node:fs';
import {{composePackedPixels, packedGeometry}} from {json.dumps(WEBGL.as_uri())};
const g = packedGeometry({json.dumps(meta)}, {geometry['encodedSize'][0]}, {geometry['halfHeight']});
const src = new Uint8ClampedArray(readFileSync({json.dumps(str(tmp_path / 'frame.raw'))}));
for (const snap of [false, true]) {{
  const out = composePackedPixels(src, g.videoWidth, g, new Uint8ClampedArray(g.width * g.height * 4), {{snap}});
  writeFileSync({json.dumps(str(tmp_path))} + (snap ? '/snap.raw' : '/raw.raw'), out);
}}
console.log(JSON.stringify(g));"""
    assert node_json(node, script)["videoWidth"] == geometry["encodedSize"][0]
    expected = av.unpack_packed_alpha(frame, geometry)
    alpha = expected[..., 3]
    shape = (geometry["height"], geometry["width"], 4)
    raw = np.frombuffer((tmp_path / "raw.raw").read_bytes(), np.uint8).reshape(shape)
    assert np.array_equal(raw[..., 3], alpha)
    assert np.array_equal(raw[..., :3][alpha > 0], expected[..., :3][alpha > 0]) and not raw[alpha == 0].any()
    snapped = np.where(alpha <= 2, 0, np.where(alpha >= 253, 255, alpha)).astype(np.uint8)
    snap = np.frombuffer((tmp_path / "snap.raw").read_bytes(), np.uint8).reshape(shape)
    assert np.array_equal(snap[..., 3], snapped)
    assert np.array_equal(snap[..., :3][snapped > 0], expected[..., :3][snapped > 0]) and not snap[snapped == 0].any()


@pytest.mark.node
def test_runtime_reads_export_engine_output(node, tmp_path):
    """Clips written by export_engine.py play back in forge-runtime.mjs with the same timing and events."""
    frames = tmp_path / "built" / "frames"
    frames.mkdir(parents=True)
    records = []
    for i in range(3):
        pixels = np.zeros((32, 24, 4), np.uint8)
        pixels[8 + i:30, 6:18] = (200, 40 * i, 90, 255)
        Image.fromarray(pixels).save(frames / f"frame-{i:02d}.png")
        records.append({"index": i, "name": f"f{i}", "file": f"frames/frame-{i:02d}.png"})
    manifest = {"schema": "generate2dsprite.animation_clips.v2", "frame_size": [24, 32], "anchor_px": [12, 30],
                "frames": records, "states": {"idle": "walk"}, "clips": {
                    "walk": {"frames": [0, 1, 2, 1], "duration_ms": [83, 84, 83, 83], "loop": True,
                             "total_duration_ms": 333, "loop_policy": "pingpong", "stride_world_units": 30,
                             "events_ms": [{"name": "step_l", "at_ms": 0, "at": 0},
                                           {"name": "step_r", "at_ms": 167, "at": 2}],
                             "tick_grid": {"hz": 60, "max_drift_ms": 0.33}},
                    "slash": {"frames": [2, 0], "duration_ms": [70, 230], "loop": False, "total_duration_ms": 300,
                              "events_ms": [{"name": "impact", "at_ms": 70, "at": 1}],
                              "tick_grid": {"hz": 60, "max_drift_ms": 0}}}}
    (tmp_path / "built" / "animation-clips.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = run_cli([script_path("generate2dsprite", "export_engine"), "--clips",
                      tmp_path / "built" / "animation-clips.json", "--output-dir", tmp_path / "engine",
                      "--target", "aseprite-json"])
    assert result.returncode == 0, result.stderr
    record = tmp_path / "engine" / "engine-export.json"
    script = f"""
import {{readFileSync}} from 'node:fs';
import * as R from {json.dumps(RUNTIME.as_uri())};
const record = JSON.parse(readFileSync({json.dumps(str(record))}, 'utf8'));
const walk = R.normalizeClip(record.clips.walk, {{name: 'walk', manifest: record}});
const slash = R.normalizeClip(record.clips.slash, {{name: 'slash', manifest: record}});
const hit = R.mapActionTime(slash, {{durationMs: 600, impactTimesMs: [250]}}, 250);
console.log(JSON.stringify({{
  walk: [walk.totalMs, walk.mode, walk.loopDistance, walk.events.map(e => [e.name, e.frame])],
  frames: [0, 82, 83, 166, 167, 250, 332, 333].map(t => R.frameAt(walk, t).frame),
  slash: [slash.totalMs, slash.mode, slash.impactMs, hit.timeMs, hit.frame],
  rect: R.anchoredRect(record, 100, 50, 2),
}}));"""
    assert node_json(node, script) == {
        "walk": [333, "cycle", 30, [["step_l", 0], ["step_r", 2]]],
        "frames": [0, 0, 1, 1, 2, 3, 3, 0],          # edges 0, 83, 167, 250, 333
        "slash": [300, "oneshot", 70, 70, 1],
        "rect": {"x": 76, "y": -10, "width": 48, "height": 64},
    }


# --------------------------------------------------------------------------- optional: real WebGL

_PAGE = """<!doctype html><meta charset="utf-8"><pre id="result">pending</pre>
<script type="module">
import {createPackedAlphaCompositor, composePackedPixels, packedGeometry} from './packed-alpha-webgl.mjs';
const out = document.getElementById('result');
try {
  const meta = {layout: 'rgb-left-alpha-right', width: 13, height: 9, halfWidth: 14, halfHeight: 10};
  const g = packedGeometry(meta);
  const source = document.createElement('canvas');
  source.width = g.videoWidth; source.height = g.videoHeight;
  const ctx = source.getContext('2d');
  const image = ctx.createImageData(g.videoWidth, g.videoHeight);
  for (let i = 0; i < image.data.length; i += 4) image.data.set([37, 37, 37, 255], i);
  const levels = [0, 1, 2, 3, 64, 128, 252, 253, 255];
  for (let y = 0; y < g.height; y++) for (let x = 0; x < g.width; x++) {
    const a = (x > 1 && x < 11 && y > 1 && y < 8) ? 255 : levels[(x + 2 * y) % levels.length];
    image.data.set([(x * 19) % 256, (y * 27) % 256, 200, 255], (y * g.videoWidth + x) * 4);
    image.data.set([a, a, a, 255], (y * g.videoWidth + g.halfWidth + x) * 4);
  }
  ctx.putImageData(image, 0, 0);
  Object.assign(source, {videoWidth: g.videoWidth, videoHeight: g.videoHeight, readyState: 4, currentTime: 0,
    seeking: false});
  const canvases = [];
  const compositor = createPackedAlphaCompositor({createCanvas: (w, h) => {
    const canvas = Object.assign(document.createElement('canvas'), {width: w, height: h});
    canvases.push(canvas);
    return canvas;
  }});
  const lease = compositor.acquire(source, meta);
  const composed = lease.update();
  // Real context loss and restore through WEBGL_lose_context: CPU frames in between, then the GPU again.
  const lose = canvases.find(c => c.getContext('2d') === null).getContext('webgl').getExtension('WEBGL_lose_context');
  const wait = () => new Promise(resolve => setTimeout(resolve, 50));
  lose.loseContext();
  await wait();
  source.currentTime = 1;
  lease.update();
  const lostMode = lease.mode;
  lose.restoreContext();
  await wait();
  source.currentTime = 2;
  lease.update();
  const restoredMode = lease.mode;
  const read = lease.drawable.getContext('2d').getImageData(0, 0, g.width, g.height).data;
  const expected = composePackedPixels(image.data, g.videoWidth, g, new Uint8ClampedArray(g.width * g.height * 4));
  let maxAlpha = 0, maxRgb = 0;
  for (let i = 0; i < expected.length; i += 4) {
    maxAlpha = Math.max(maxAlpha, Math.abs(read[i + 3] - expected[i + 3]));
    if (expected[i + 3] < 64) continue;
    for (let c = 0; c < 3; c++) maxRgb = Math.max(maxRgb, Math.abs(read[i + c] - expected[i + c]));
  }
  const outlined = compositor.acquire(source, meta, {outline: true});
  outlined.update(true);
  const ring = outlined.drawable.getContext('2d').getImageData(0, 0, g.width, g.height).data;
  let grown = 0;
  for (let i = 0; i < ring.length; i += 4) if (expected[i + 3] === 0 && ring[i + 3] > 0) grown++;
  const stats = compositor.getStats();
  out.textContent = JSON.stringify({composed, mode: lease.mode, outlineMode: outlined.mode, maxAlpha, maxRgb, grown,
    lostMode, restoredMode, losses: stats.contextLosses, restores: stats.contextRestores, errors: stats.errors});
} catch (error) {
  out.textContent = JSON.stringify({error: String(error && error.stack || error)});
}
</script>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript"}

    def log_message(self, *args) -> None:
        pass


def test_webgl_compositor_in_a_headless_browser(tmp_path):
    browser = os.environ.get("FORGE_BROWSER")
    if not browser:
        pytest.skip("set FORGE_BROWSER to a Chrome or Edge executable to run the optional WebGL check")
    site = tmp_path / "site"
    site.mkdir()
    for source in (RUNTIME, WEBGL):
        shutil.copyfile(source, site / source.name)
    (site / "index.html").write_text(_PAGE, encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [browser, "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             f"--user-data-dir={tmp_path / 'profile'}", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
             "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{server.server_address[1]}/index.html"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    finally:
        server.shutdown()
        server.server_close()
    match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stdout[-2000:] + result.stderr[-2000:]
    report = json.loads(html.unescape(match.group(1)))
    assert "error" not in report, report
    assert report["composed"] is True and report["mode"] == "webgl" and report["outlineMode"] == "webgl", report
    # Alpha is exact after the snap; colour read back from an 8-bit premultiplied canvas may differ by
    # the quantization at alpha >= 64 (measured 2 on SwiftShader).
    assert report["maxAlpha"] == 0 and report["maxRgb"] <= 3, report
    assert report["grown"] > 0, report
    recovery = (report["lostMode"], report["restoredMode"], report["losses"], report["restores"])
    assert recovery == ("cpu", "webgl", 1, 1), report
