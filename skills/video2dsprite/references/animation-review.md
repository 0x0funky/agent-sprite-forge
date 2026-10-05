# Review source frames before choosing a cycle

`scripts/animation_review.py` makes a local HTML reviewer from common-canvas RGBA
PNGs. It works on cleaned video frames or prepared sheet frames. It never generates,
interpolates, aligns feet, or alters source files. The reduced preview preserves the
canvas registration; inspect originals at runtime resolution for final edge quality.

```bash
python skills/video2dsprite/scripts/animation_review.py review --frames-dir work/frames-clean --out-dir work/review --fps 24 --min-cycle 8 --max-cycle 36
```

Open `work/review/index.html`. Review a moving cycle at normal speed, then pause,
step, and scrub. Switch light/dark/checker backgrounds to see key spill. Set the
ground annotation from the source art and compare contact with intended flight.
The frame count and FPS describe the supplied samples, not an artistic quality score.

Candidate intervals require a second repeated window and rank recurrence plus
boundary change. They can suggest a partial gait, a doubled gait or repeated model
deformation. Compare alternatives; don't accept the first numeric result blindly.
An attack may correctly have no repeated candidate: export it as a one-shot.
Exact duplicate diagnostics ignore invisible RGB; largest-step indices locate
review points, including intentional impacts which must not be automatically removed.

Select start/end (end is exclusive), then **Save selection JSON**. Exporting verifies
the source-directory binding and every selected source hash. Re-review if the source
changes rather than applying an old cut to different frames.

```bash
python skills/video2dsprite/scripts/animation_review.py cut --frames-dir work/frames-clean --selection frame-selection.json --out-dir work/selected
python skills/video2dsprite/scripts/video2dsprite.py package --clean-dir work/selected --out-dir assets/hero-run --name hero-run --fps 24 --formats png,webm,packed --loop
```

Add the actual source geometry/anchor to packaging. Alternatively a reviewed interval
can be exported using `cut --start 12 --end 28 --fps 24` without a selection JSON.
The cut preserves PNG bytes and timing and refuses an existing destination. Neither
selection nor export changes `needs-visual-review` into a production approval.

No RIFE dependency is required here. If separately using interpolation, disclose the
derived frames, retain originals, and inspect hands, silhouette, pixel-grid stability
and contacts. More samples cannot recover a missing authored pose or semantic intent.
