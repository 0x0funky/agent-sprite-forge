# Validation record — 2026-10-05

Local working-tree update, not a published release. Baseline commit: `64fd0b5`.
No API credits were spent; no global skills, game deployment or wiki were modified.

## Automated coverage

- Baseline: 15 tests passed before edits.
- Updated suite: **105 tests passed**, including 11 subtests.
- All four skill metadata validations passed.
- Skill metadata and relative reference links are checked in the test suite.
- Python compilation and the packed-alpha JavaScript module syntax check passed.
- `git diff --check` passed.

Run locally:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q skills
```

Tests cover alpha preservation, native-alpha and chroma modes, connected components,
crop/grid rejection, strict output publication, rectangular structural props,
parallax coverage, source geometry, real ffmpeg codecs, provider request shapes,
one-submit/no-retry behavior and interrupted video-job recovery. Provider HTTP is
mocked: successful offline tests do not establish live model access or generated
art quality.

## Independent task-based review

A separate agent exercised the API/video user paths without implementation hints.
It found three issues, all fixed and re-tested with the same reproductions:

1. A video downloaded just before job-metadata commit could not resume. Existing
   output is now adopted only if identical to the same job's downloaded bytes.
   Conflicting files remain untouched; paid submission is never repeated.
2. Union crop reused a diagnostic alpha threshold and clipped faint glow. Crop
   geometry now includes **all nonzero alpha**; diagnostic thresholds stay separate.
3. Repackaging could overwrite accepted outputs or leave partial files. A fresh
   destination and staged publication now protect accepted output.

Independent sprite CLI checks also verified original PNG hashes, semi-transparent
purple preservation, shared anchors, variable/duplicate-hold timing, timed
single-frame one-shots and complete-frame assembly. These were local behavioral
tests, not checks for matching documentation wording.

## Existing-media end-to-end smoke

The checked-in `src/video2dsprite-ryo/run-6s.mp4` was decoded over a one-second
interval, sampled at 12fps, keyed, and packaged with a fixed source canvas and
explicit anchor. Both VP9-alpha and packed-H.264 were encoded and fully decoded.
Poster and paged PNG fallback were generated. Work artifacts live under ignored
`outputs/upgrade-smoke-20261005/`; they are not additional repository download weight.

The source is 480×480. The test package retains that source geometry while encoding
a shared union region capped at 256px. This is a **packaging smoke test**, not a new
motion-quality showcase. The old Ryo clip still has some keyed edge color; optional
one-pixel despill is limited and does not replace visual review. The chosen interval
and root coordinates are test inputs, not a newly approved production gait.

## What remains unproven

- Live API credentials, model access, latency, quality and final billed amounts.
- Exact native-alpha support and frame time on real iPhone/Safari devices.
- Seamless loops, anatomy, foot contact or good art solely from numeric QA.
- Performance of the CPU packed-alpha demo at many concurrent characters. It is
  a correctness example; a shared GPU compositor and measured decoder budget are
  required for heavier scenes.
- Full integration into Unity/Godot/Three.js games. Those are downstream tasks.

No browser/gameplay benchmark was substituted for these missing checks.
