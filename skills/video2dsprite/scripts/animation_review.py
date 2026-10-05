"""Offline, non-destructive frame review and explicit interval export.

  review  local HTML reviewer with recurrence candidate intervals
  select  classify candidate intervals (valid 1-cycle, valid 2-cycle, rejected with the
          reasons) with gait_loop's period analysis; writes a v2 frame selection
  cut     byte-preserved export of a v1 interval or a v2 selection (holds, retimed order)

Candidate intervals are recurrence diagnostics, never automatic gait approval.
No interpolation, per-frame normalization, generation, or network access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import gait_loop  # noqa: E402  (period analysis and frame selection v2)


def sources(directory: Path) -> list[Path]:
    paths = sorted(directory.glob("*.png"))
    if not paths or len(paths) > 600:
        raise ValueError("expected 1..600 lexically ordered PNG frames")
    size = None
    for path in paths:
        with Image.open(path) as im:
            if im.format != "PNG" or im.mode != "RGBA" or getattr(im, "n_frames", 1) != 1:
                raise ValueError("frames must be still RGBA PNGs")
            size = size or im.size
            if im.size != size:
                raise ValueError("frames require a common source canvas")
            if im.width * im.height > 16_000_000:
                raise ValueError("frame exceeds 16 million pixels")
    return paths


def signature(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def features(paths: list[Path]) -> tuple[np.ndarray, list, list]:
    arrays, bounds, hashes = [], [], []
    for path in paths:
        with Image.open(path) as im:
            bounds.append(im.getchannel("A").getbbox())
            small = im.copy()
            small.thumbnail((64, 64), Image.Resampling.LANCZOS)
            a = np.asarray(small, dtype=np.float32) / 255
            a[..., :3] *= a[..., 3:4]
            arrays.append(a)
            # Invisible RGB cannot make an otherwise identical frame unique.
            full = np.array(im)
            full[full[..., 3] == 0, :3] = 0
            hashes.append(hashlib.sha256(full.tobytes()).hexdigest())
    return np.stack(arrays), bounds, hashes


def candidates(values: np.ndarray, minimum: int, maximum: int, limit: int = 8) -> list[dict]:
    """Rank repeated local windows, retaining different lengths for human review.

    Requires a second complete window as recurrence evidence. A half-gait or a
    changing identity can also recur; this deliberately does not name a true period.
    """
    if minimum < 2 or maximum < minimum:
        raise ValueError("cycle bounds require 2 <= minimum <= maximum")
    count = len(values)
    axes = tuple(range(1, values.ndim))
    adjacent = np.abs(np.diff(values, axis=0)).mean(axis=axes)
    ranked = []
    for length in range(minimum, min(maximum, count // 2) + 1):
        repeat = np.abs(values[length:] - values[:-length]).mean(axis=axes)
        for start in range(count - 2 * length + 1):
            typical = float(np.median(adjacent[start:start + length - 1]))
            if typical < 1e-6:
                continue  # a static hold is not evidence of a moving cycle
            end = start + length
            seam_step = values[start] - values[end - 1]
            seam = float(np.abs(seam_step).mean())
            repeat_error = float(repeat[start:end].mean())
            if repeat_error >= typical:
                continue  # recurrence must improve on ordinary adjacent change
            near_steps = ((values[start + 1] - values[start]) +
                          (values[end - 1] - values[end - 2])) / 2
            turn = float(np.abs(seam_step - near_steps).mean())
            score = repeat_error / typical + .25 * abs(seam / typical - 1) + .1 * turn / typical
            ranked.append({"start": start, "endExclusive": end, "frameCount": length,
                           "recurrenceMAE": round(repeat_error, 7),
                           "seamMAE": round(seam, 7), "adjacentMedianMAE": round(typical, 7),
                           "recurrenceToMedianRatio": round(repeat_error / typical, 3),
                           "seamToMedianRatio": round(seam / typical, 3),
                           "rankingScore": round(score, 5)})
    ranked.sort(key=lambda item: item["rankingScore"])
    selected = []
    for item in ranked:
        # Show alternative durations, not eight near-identical starts of one cut.
        if any(abs(item["frameCount"] - other["frameCount"]) <= 1 for other in selected):
            continue
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forge animation review</title><style>
*{box-sizing:border-box}body{margin:0;background:#101820;color:#dae5ec;font:15px system-ui;padding:24px;max-width:1180px;margin:auto}
h1{font-size:26px;margin:0 0 8px}p{line-height:1.6;color:#a9bdc9}main{display:grid;grid-template-columns:minmax(280px,1fr) 1fr;gap:24px}
canvas{width:100%;height:auto;image-rendering:pixelated;background:#eee;border:1px solid #668}
button,select,input{background:#253644;color:inherit;border:1px solid #62798a;border-radius:6px;padding:8px;margin:4px}button{cursor:pointer}input[type=number]{width:94px}input[type=range]{width:95%;padding:0}
button:hover{background:#456172}.options{display:flex;flex-wrap:wrap}.candidate{display:block;text-align:left;width:100%}pre{white-space:pre-wrap;background:#1c2c37;padding:12px;overflow-wrap:anywhere;font-size:12px}small{color:#aac2ce}
@media(max-width:700px){main{grid-template-columns:1fr}body{padding:12px}}
</style><h1>Forge · Animation review</h1><p>Reduced preview, original registration. Candidates need visual review: repeated pixels cannot prove correct feet, identity, or a full gait. Source files stay unchanged.</p>
<main><section><canvas id="view" width="512" height="512"></canvas><input id="scrub" type="range" min="0" value="0" step="1">
<div class="options"><button id="play">Pause</button><button id="prev">← Frame</button><button id="next">Frame →</button><select id="back"><option value="checker">Checker</option><option value="light">Light</option><option value="dark">Dark</option></select><select id="speed"><option value="1">1× timing</option><option value="0.5">0.5× timing</option><option value="0.25">0.25× timing</option></select></div><p id="readout"></p><small>Ground reference in source pixels (annotation only)</small><input id="ground" type="number" min="0"><br><label><input id="bounds" type="checkbox"> Show alpha bounds</label>
</section><section><h2>Playback interval</h2><label>Start <input id="start" type="number" min="0" step="1"></label><label>End (exclusive) <input id="end" type="number" min="1" step="1"></label><button id="whole">Whole clip</button><button id="save">Save selection JSON</button><p id="span"></p><h2>Candidate intervals</h2><div id="candidates"></div><h2>Diagnostics</h2><pre id="diagnostics"></pre><small>Source FPS is declared by --fps. Playback speed is only a review control. Delayed display ticks may skip drawings to retain source timing. Selecting a range does not normalize, interpolate, or certify a loop.</small></section></main>
<script>const data=__DATA__; const $=id=>document.getElementById(id);let index=0,running=true,elapsed=0,last=performance.now(),loaded=false;
const imgs=data.frames.map(f=>{const im=new Image();im.src=f.preview;return im});
Promise.all(imgs.map(im=>im.decode())).then(()=>loaded=true).catch(()=>{$('readout').textContent='Preview image decode failed';});
$('scrub').max=imgs.length-1;$('start').value=0;$('end').value=imgs.length;$('ground').value=data.sourceSize[1]*.9;
const ctx=$('view').getContext('2d');ctx.imageSmoothingEnabled=false;
function integerValue(id,fallback){const raw=$(id).value;const value=raw===''?fallback:Number(raw);return Number.isFinite(value)?Math.trunc(value):fallback}
function range(){let a=Math.max(0,Math.min(imgs.length-1,integerValue('start',0)));let b=Math.max(a+1,Math.min(imgs.length,integerValue('end',imgs.length)));return[a,b]}
function change(){let[a,b]=range();$('start').value=a;$('end').value=b;index=a;elapsed=0;last=performance.now();$('span').textContent=`${b-a} frames · ${((b-a)/data.fps).toFixed(3)} s at ${data.fps} fps`}
function selection(){let[a,b]=range();return{schema:'forge-frame-selection/v1',sourceDirectory:data.sourceDirectoryName||data.sourceDirectory,start:a,endExclusive:b,fps:data.fps,sourceHashes:data.frames.slice(a,b).map(f=>f.sha256),status:'selected-needs-visual-review'}}
$('start').onchange=$('end').onchange=change;$('whole').onclick=()=>{$('start').value=0;$('end').value=imgs.length;change()};
$('scrub').oninput=()=>{running=false;index=Number($('scrub').value);$('play').textContent='Play'};
$('play').onclick=()=>{running=!running;$('play').textContent=running?'Pause':'Play';elapsed=0;last=performance.now();const[a,b]=range();if(running&&(index<a||index>=b))index=a};
for(const[id,step]of[['prev',-1],['next',1]])$(id).onclick=()=>{running=false;const[a,b]=range();index=index<a||index>=b?a:a+(index-a+step+b-a)%(b-a);elapsed=0;$('play').textContent='Play'};
$('save').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(selection(),null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='frame-selection.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
for(const c of data.candidates){let b=document.createElement('button');b.className='candidate';b.textContent=`${c.start}–${c.endExclusive-1} · ${c.frameCount} frames · ${(c.frameCount/data.fps).toFixed(3)} s · repeat/step ${c.recurrenceToMedianRatio} · seam/step ${c.seamToMedianRatio}`;b.onclick=()=>{$('start').value=c.start;$('end').value=c.endExclusive;change()};$('candidates').append(b)}
if(!data.candidates.length)$('candidates').textContent='No moving recurrence candidate in the supplied length range. Choose manually; do not force a loop.';
$('diagnostics').textContent=JSON.stringify(data.diagnostics,null,2);change();
function tick(now){const dt=Math.max(0,now-last);last=now;if(loaded){const[a,b]=range();if(running){if(index<a||index>=b)index=a;elapsed+=dt*Number($('speed').value);const stepMs=1000/data.fps;const steps=Math.floor(elapsed/stepMs);elapsed-=steps*stepMs;index=a+(index-a+steps)%(b-a)}const w=512,h=512;const bg=$('back').value;ctx.fillStyle=bg==='dark'?'#15202d':'#eee';ctx.fillRect(0,0,w,h);if(bg==='checker'){ctx.fillStyle='#c3cbd1';for(let y=0;y<h;y+=24)for(let x=0;x<w;x+=24)if(((x/24+y/24)%2)===0)ctx.fillRect(x,y,24,24)}const s=Math.min(w/data.sourceSize[0],h/data.sourceSize[1]);const dw=data.sourceSize[0]*s,dh=data.sourceSize[1]*s,ox=(w-dw)/2,oy=(h-dh)/2;ctx.drawImage(imgs[index],ox,oy,dw,dh);ctx.strokeStyle='#ed654f';ctx.lineWidth=1;ctx.beginPath();let gy=oy+Number($('ground').value)*s;ctx.moveTo(ox,gy);ctx.lineTo(ox+dw,gy);ctx.stroke();const bb=data.frames[index].bounds;if($('bounds').checked&&bb){ctx.strokeStyle='#219fda';ctx.strokeRect(ox+bb[0]*s,oy+bb[1]*s,(bb[2]-bb[0])*s,(bb[3]-bb[1])*s)}$('scrub').value=index;$('readout').textContent=`Frame ${index} / ${imgs.length-1} · ${(index/data.fps).toFixed(3)} s · source ${data.sourceSize.join(' × ')} · preview cap ${data.previewMaxSide}px`;}
requestAnimationFrame(tick)}requestAnimationFrame(tick);</script></html>'''


def review(directory: Path, out: Path, fps: float, minimum: int, maximum: int, preview_size: int = 192) -> dict:
    if not math.isfinite(fps) or fps <= 0 or fps > 240 or not 32 <= preview_size <= 512:
        raise ValueError("fps must be in (0,240], preview size in 32..512")
    if out.exists():
        raise ValueError("output directory already exists")
    paths = sources(directory)
    source_hashes = [signature(path) for path in paths]
    values, bounds, hashes = features(paths)
    proposed = candidates(values, minimum, maximum)
    adjacent = np.abs(np.diff(values, axis=0)).mean(axis=tuple(range(1, values.ndim)))
    with Image.open(paths[0]) as im:
        size = list(im.size)
    # Paths are relative to the review folder, or only the name across drives (forge_core.manifest_path; the
    # sha256 values are the binding). The reviewer page saves selections with the bare folder name: a
    # downloaded file has no known location, and cut checks the hashes (r2-conventions finding 5).
    report = {"schema": "forge-animation-review/v1", "status": "needs-visual-review",
              "sourceDirectory": forge_core.manifest_path(directory, out),
              "sourceDirectoryName": directory.resolve().name, "sourceSize": size,
              "fps": fps, "previewMaxSide": preview_size, "candidates": proposed,
              "diagnostics": {"frameCount": len(paths), "durationSeconds": len(paths) / fps,
                              "uniqueVisibleFrames": len(set(hashes)),
                              "blankFrames": [i for i, b in enumerate(bounds) if b is None],
                              "adjacentExactDuplicates": [i + 1 for i in range(len(hashes)-1) if hashes[i] == hashes[i+1]],
                              "largestSteps": [{"from": int(i), "to": int(i+1), "MAE": round(float(adjacent[i]), 7)} for i in np.argsort(adjacent)[-8:][::-1]],
                              "candidateGate": "recurrenceMAE < adjacentMedianMAE; heuristic improvement over ordinary frame-to-frame change, not loop approval",
                              "limits": "Recurrence is not gait/contact/identity validation. Candidate lengths may be partial or multiple cycles. No interpolation or registration repair applied."},
              "frames": [{"source": forge_core.manifest_path(p, out), "sha256": source_hashes[i], "bounds": bounds[i],
                          "preview": f"frames/frame-{i:04d}.png"} for i, p in enumerate(paths)]}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".review-", dir=out.parent) as tmp:
        stage = Path(tmp) / "bundle"
        (stage / "frames").mkdir(parents=True)
        for i, path in enumerate(paths):
            with Image.open(path) as im:
                im.thumbnail((preview_size, preview_size), Image.Resampling.LANCZOS)
                # Straight alpha with RGB zeroed under alpha 0 (Appendix D): LANCZOS ringing leaves colour there.
                forge_core.save_png(im, stage / report["frames"][i]["preview"])
        if [signature(path) for path in paths] != source_hashes:
            raise ValueError("source frames changed during review; no review published")
        (stage / "review.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        inline = json.dumps(report).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        (stage / "index.html").write_text(HTML.replace("__DATA__", inline), encoding="utf-8")
        stage.rename(out)
    return report


def cut(directory: Path, out: Path, start: int, end: int, fps: float, expected: list[str] | None = None) -> dict:
    if out.exists():
        raise ValueError("output directory already exists")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 < fps <= 240:
        raise ValueError("positive finite fps <=240 required")
    if type(start) is not int or type(end) is not int:
        raise ValueError("start and end must be integers, not booleans or fractional values")
    if expected is not None and (
        not isinstance(expected, list) or len(expected) != end - start or
        any(not isinstance(digest, str) or len(digest) != 64 or
            any(c not in "0123456789abcdef" for c in digest) for digest in expected)
    ):
        raise ValueError("selection hashes must be one lowercase SHA-256 string per selected frame")
    paths = sources(directory)
    if not 0 <= start < end <= len(paths):
        raise ValueError("expected 0 <= start < end <= frame count")
    paths = paths[start:end]
    digests = [signature(p) for p in paths]
    if expected is not None and expected != digests:
        raise ValueError("selection hashes no longer match source frames")
    result = {"schema": "forge-frame-cut/v1", "sourceDirectory": forge_core.manifest_path(directory, out),
              "start": start, "endExclusive": end, "fps": fps, "durationSeconds": len(paths)/fps,
              "status": "selected-needs-visual-review", "sourceHashes": digests,
              "transforms": "none; byte-preserved shared canvases"}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cut-", dir=out.parent) as tmp:
        stage = Path(tmp) / "bundle"
        stage.mkdir()
        for i, path in enumerate(paths):
            copied = stage / f"frame-{i:04d}.png"
            shutil.copyfile(path, copied)
            if signature(copied) != digests[i]:
                raise ValueError("source frames changed during copy; no cut published")
        (stage / "selection.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        stage.rename(out)
    return result


CLASS_ORDER = ("valid-1-cycle", "valid-2-cycle", "valid-cycle", "rejected")
ENTRY_METRICS = ("seconds", "files", "covers", "strides", "phase_error_frames_local", "context_repeat_ratio",
                 "wrap_ratio", "root_drift_pct_body", "step_asymmetry", "closure_steps", "amplitude_steps", "score")


def _entry(window: dict, source: str, recurrence: dict | None = None) -> dict:
    verdict = window["classification"]
    entry = {"start": window["start"], "endExclusive": window["endExclusive"], "frameCount": window["frames"],
             "class": verdict["class"], "reasons": verdict["reasons"], "notes": verdict["notes"], "source": source,
             "metrics": {key: window[key] for key in ENTRY_METRICS if key in window}}
    if recurrence is not None:
        entry["recurrence"] = {key: recurrence[key] for key in ("recurrenceToMedianRatio", "seamToMedianRatio",
                                                                "rankingScore")}
    return entry


def _rank(entry: dict) -> tuple:
    """Valid classes first, then the lower (better) seam or closure score."""
    score = entry["metrics"].get("score")
    return CLASS_ORDER.index(entry["class"]), math.inf if score is None else score, entry["start"]


def select(directory: Path, out: Path, fps, kind: str = "gait", state: str = "run", minimum: int = 6,
           maximum: int = 48, limit: int = 8) -> dict:
    """Classify the recurrence candidates and gait_loop's best windows; write a v2 selection.

    Each candidate becomes valid-1-cycle or valid-2-cycle (gait), valid-cycle (idle, hover)
    or rejected with every reason (unusable frames, half or partial stride, seam, wrap,
    drift range). selection.json holds the recommended loop and candidates.json the
    classified list. Nothing is published when no window is a valid loop.
    """
    if out.exists():
        raise ValueError("output directory already exists")
    if not 1 <= limit <= 32:
        raise ValueError("--limit must be in 1..32")
    paths = sources(directory)
    values, _, _ = features(paths)
    proposed = candidates(values, minimum, maximum, limit)
    analysis = gait_loop.analyse(directory, fps, kind, state)
    entries = [_entry(gait_loop.classify_window(analysis, c["start"], c["endExclusive"]), "recurrence", c)
               for c in proposed]
    seen = {(e["start"], e["endExclusive"]) for e in entries}
    try:
        rec = gait_loop.recommend(analysis)
    except ValueError as error:
        rec, failure = None, str(error)
    if rec is not None:
        for windows in rec["candidates"].values():
            for window in windows:
                if (window["start"], window["endExclusive"]) not in seen:
                    seen.add((window["start"], window["endExclusive"]))
                    entries.append(_entry(window, "gait_loop"))
        window, policy = rec["window"], rec["policy"]
    else:
        valid = [e for e in entries if e["class"] != "rejected"]
        if not valid:
            reasons = "; ".join(f"{e['start']}:{e['endExclusive']} {e['reasons'][0]}" for e in entries[:4])
            raise ValueError(f"no valid loop ({failure})" + (f"; candidates rejected: {reasons}" if reasons else ""))
        best = min(valid, key=_rank)
        window, policy = gait_loop.classify_window(analysis, best["start"], best["endExclusive"]), "cycle"
    entries.sort(key=_rank)
    indices = gait_loop.kept_indices(analysis, window["start"], window["endExclusive"])
    durations = gait_loop.kept_durations(window["start"], window["endExclusive"], indices, analysis.clip.fps)
    seam = gait_loop.sequence_seam(analysis.clip, indices + indices[-2:0:-1] if policy == "pingpong" else indices)
    if [signature(path) for path in paths] != analysis.clip.hashes:
        raise ValueError("source frames changed during select; nothing published")
    counts = {name: sum(e["class"] == name for e in entries) for name in CLASS_ORDER}
    review_text = (gait_loop.REVIEW_FIRST + " Candidates are classified by pixel evidence; "
                   "rejected ones say why. Confirm the chosen loop visually.")
    with forge_core.staged_output(out) as stage:
        selection = gait_loop.build_selection(
            analysis.clip, stage, indices, durations, policy=policy, kind=kind, review=review_text,
            window=(window["start"], window["endExclusive"]),
            method=f"animation_review select ({kind}): recurrence candidates classified by gait_loop",
            extra={"cycles": window.get("strides", 1), "seam": seam})
        report = {"schema": "forge-animation-select/v1", "status": "needs-visual-review",
                  "sourceDirectory": gait_loop.directory_ref(directory, stage),
                  "fps": gait_loop.fps_text(analysis.clip.fps), "kind": kind,
                  "period": ({key: analysis.period[key] for key in ("T_frames", "S_frames", "verdict", "why")}
                             if analysis.period else None),
                  "unusableFrames": analysis.unusable,
                  "usableRange": {key: analysis.usable[key] for key in ("start", "endExclusive", "reason")},
                  "recommendation": {"start": window["start"], "endExclusive": window["endExclusive"],
                                     "policy": policy, "sourceIndices": indices,
                                     "confidence": rec["confidence"] if rec else None},
                  "counts": counts, "candidates": entries,
                  "candidateSearch": {"minCycle": minimum, "maxCycle": maximum, "limit": limit,
                                      "gate": "recurrenceMAE < adjacentMedianMAE (review helper), then gait_loop "
                                              "classification"}}
        gait_loop.write_selection_files(stage, {"selection.json": selection})
        report["qa"] = gait_loop.qa_envelope(
            "needs-visual-review", selection["method"],
            [{"id": "valid-loop", "status": "pass", "value": [window["start"], window["endExclusive"]]},
             {"id": "rejected-candidates-explained", "status": "pass",
              "value": sum(bool(e["reasons"]) for e in entries if e["class"] == "rejected"),
              "threshold": counts["rejected"]},
             {"id": "visual-review", "status": "needs-visual-review", "value": None}],
            gait_loop.frame_refs(analysis.clip, stage, range(analysis.clip.count)),
            [gait_loop.file_ref(stage / "selection.json", stage)],
            tool={"name": "animation_review select", "version": gait_loop.GAIT_LOOP_VERSION})
        gait_loop.write_selection_files(stage, {"candidates.json": report})
    return {"status": gait_loop.SELECTED_STATUS, "counts": counts,
            "recommendation": [window["start"], window["endExclusive"]]}


def cut_selection(directory: Path, out: Path, selection_path: Path) -> dict:
    """Byte-preserved export of a forge-frame-selection/v2: frames in played order (holds and
    reversals repeat the same bytes) plus selection.json rebased onto the cut folder."""
    if out.exists():
        raise ValueError("output directory already exists")
    selection = gait_loop.read_selection(selection_path)
    if selection["schema"] != gait_loop.SELECTION_V2:
        raise ValueError("cut_selection reads forge-frame-selection/v2")
    paths = sources(directory)
    gait_loop.verify_selection(selection, selection_path, directory)
    indices, durations, raw = selection["indices"], selection["durations"], selection["raw"]
    digests = {index: selection["hashes"][index - selection["start"]] for index in set(indices)}
    with forge_core.staged_output(out) as stage:
        names = []
        for position, index in enumerate(indices):
            copied = stage / f"frame-{position:04d}.png"
            shutil.copyfile(paths[index], copied)
            if signature(copied) != digests[index]:
                raise ValueError("source frames changed during copy; no cut published")
            names.append(copied.name)
        rebased = {**raw, "sourceDirectory": ".", "sourceHashes": [digests[i] for i in indices], "sourceFiles": names,
                   "start": 0, "endExclusive": len(indices), "sourceIndices": list(range(len(indices))),
                   "durations_ms": durations, "fps": forge_core.rational_fps(len(indices), sum(durations)),
                   "status": gait_loop.SELECTED_STATUS,
                   "method": f"animation_review cut (byte-preserved, played order) of: {raw['method']}",
                   "origin": {"sourceDirectory": gait_loop.directory_ref(directory, out),
                              "start": selection["start"], "endExclusive": selection["endExclusive"],
                              "sourceIndices": indices, "selection": gait_loop.file_ref(selection_path, out),
                              "transforms": "none; byte-preserved shared canvases"}}
        gait_loop.write_selection_files(stage, {"selection.json": rebased})
    return rebased


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    rv = sub.add_parser("review")
    rv.add_argument("--frames-dir", type=Path, required=True)
    rv.add_argument("--out-dir", type=Path, required=True)
    rv.add_argument("--fps", type=float, required=True)
    rv.add_argument("--min-cycle", type=int, default=6)
    rv.add_argument("--max-cycle", type=int, default=48)
    rv.add_argument("--preview-size", type=int, default=192)
    ct = sub.add_parser("cut")
    ct.add_argument("--frames-dir", type=Path, required=True)
    ct.add_argument("--out-dir", type=Path, required=True)
    ct.add_argument("--start", type=int)
    ct.add_argument("--end", type=int, help="exclusive")
    ct.add_argument("--fps", type=float)
    ct.add_argument("--selection", type=Path,
                    help="selection JSON (v1 from the reviewer, or v2 from select, gait_loop or retime); "
                         "validates source hashes")
    sl = sub.add_parser("select", help="classify candidate intervals and write a v2 frame selection")
    sl.add_argument("--frames-dir", type=Path, required=True)
    sl.add_argument("--output-dir", "--out-dir", dest="out_dir", type=Path, required=True,
                    help="new directory for selection.json and candidates.json")
    sl.add_argument("--fps", required=True, help="source frame rate: a number or N/D")
    sl.add_argument("--kind", choices=gait_loop.KINDS, default="gait")
    sl.add_argument("--state", choices=gait_loop.STATES, default="run", help="gait stride window (default run)")
    sl.add_argument("--min-cycle", type=int, default=6)
    sl.add_argument("--max-cycle", type=int, default=48)
    sl.add_argument("--limit", type=int, default=8, help="recurrence candidates to classify (default 8)")
    return parser


def _run(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "review":
        result = review(args.frames_dir, args.out_dir, args.fps, args.min_cycle, args.max_cycle, args.preview_size)
        metadata = args.out_dir / "review.json"
    elif args.command == "select":
        result = select(args.frames_dir, args.out_dir, args.fps, args.kind, args.state, args.min_cycle,
                        args.max_cycle, args.limit)
        print(json.dumps({"output": str(args.out_dir.resolve()),
                          "selection": str((args.out_dir / "selection.json").resolve()),
                          "metadata": str((args.out_dir / "candidates.json").resolve()),
                          "status": result["status"], "recommended": result["recommendation"],
                          "counts": result["counts"]}, ensure_ascii=True))
        return 0
    else:
        expected = data = None
        if args.selection:
            if any(v is not None for v in (args.start, args.end, args.fps)):
                raise ValueError("use selection OR start/end/fps, not both")
            data = forge_core.read_json(args.selection, strict=True)  # UTF-8 with or without a BOM (D28)
            if not isinstance(data, dict):
                raise ValueError("selection must be a JSON object")
        if data is not None and data.get("schema") == gait_loop.SELECTION_V2:
            result = cut_selection(args.frames_dir, args.out_dir, args.selection)
        else:
            if data is not None:
                if not isinstance(data.get("sourceDirectory"), str) or not data["sourceDirectory"].strip():
                    raise ValueError("selection sourceDirectory must be a nonempty path string")
                # Old v1 files hold an absolute path; new ones a path relative to the selection file or only
                # the folder name (the reviewer page), bound by the source hashes (gait_loop.resolve_directory).
                if data.get("schema") != "forge-frame-selection/v1" or not gait_loop.resolve_directory(
                        data["sourceDirectory"], args.selection, args.frames_dir):
                    raise ValueError("selection schema/source directory mismatch")
                args.start, args.end, args.fps = data["start"], data["endExclusive"], data["fps"]
                expected = data["sourceHashes"]
                if not isinstance(expected, list):
                    raise ValueError("selection sourceHashes must be an array; hash validation cannot be omitted")
            if any(v is None for v in (args.start, args.end, args.fps)):
                raise ValueError("cut requires --selection or --start/--end/--fps")
            result = cut(args.frames_dir, args.out_dir, args.start, args.end, args.fps, expected)
        metadata = args.out_dir / "selection.json"
    print(json.dumps({"output": str(args.out_dir.resolve()), "metadata": str(metadata.resolve()),
                      "status": result["status"]}, ensure_ascii=True))
    return 0


def main(argv=None) -> int:
    """Refused input prints ``error: ...`` and anything unexpected ``error: internal error (...)`` (D27); both
    leave through SystemExit(1), as the review and cut verbs always did. Usage errors exit 2."""
    code = forge_core.run_cli(_run, argv, expected=forge_core.CLI_EXPECTED_ERRORS + (KeyError, TypeError))
    if code:
        raise SystemExit(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
