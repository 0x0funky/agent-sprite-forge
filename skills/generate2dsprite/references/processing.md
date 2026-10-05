# Processor and export contracts

Commands below run from the skill directory. Paths and dimensions are examples; use the measured input and a **new** output directory. Python dependencies are in the repository requirements.

## Isolated sprites / normalization

```bash
python scripts/generate2dsprite.py process \
  --input accepted-idle.png --target player --mode idle --rows 2 --cols 3 \
  --output-dir output/hero-idle-v2 --cell-size 128 --fit-scale 0.80 \
  --background-mode native_alpha --resampler nearest \
  --align feet --scale-strategy preserve --component-mode largest \
  --strict-qc --max-body-scale-cv 0.08 --max-anchor-y-std 0.05 \
  --write-scale-profile output/hero-scale.json
```

- `native_alpha` requires visible pixels plus real transparency, preserves RGBA and bypasses chroma trim/edge cleanup. It rejects fully opaque checkerboard art. `opaque` requires opaque pixels. `chroma_key` removes magenta; old CLI default remains `chroma_key`, not auto-detection.
- `nearest` preserves pixel colors; `lanczos` remains the legacy default for smooth art. A shared scale profile locks these settings for compatible actions.
- Chroma border trimming/edge cleanup can remove artwork. Use `--trim-border 0 --edge-clean-depth 0` when the clean source needs no border repair; inspect before enabling legacy cleanup. `--despill-radius 1..3` is optional and may alter intentional purple near transparent edges. Default 0 is off.
- `preserve` uses a uniform raw-cell scale then translates subjects to the selected anchor. `fit --shared-scale` uses one bbox-derived scale within a sheet; it does not guarantee the same scale across separate sheets. Do not fit registered run/jump frames to their feet.
- `largest` keeps only the selected connected component, including inside its bounding box; disconnected equipment/limbs may be removed. Inspect it. `all` preserves intentional components, with `--min-component-area` as an explicit noise threshold.
- Rows/columns must divide source dimensions exactly; a generated 1672x941 image cannot silently lose its last row in a 2x2 split. Measure/correct geometry explicitly or use verified crop boxes with the full-frame assembler.
- Later compatible actions use `--scale-profile output/hero-scale.json`. Old v1 profiles remain readable. Do not issue an independent profile per action to conceal model scale drift.

The processor exports raw/clean source, transparent sheet, frame PNGs, GIF preview, prompt when supplied, and `pipeline-meta.json`. GIF is a limited-color, hard-alpha review format; PNGs remain the alpha source of truth. Strict failure publishes no finished output directory. Existing destinations are refused, preventing stale files from mixing into revisions.

### QC and action-specific gates

Inspect output edge touches, empty frames, paste clamping, anatomy and correct root at gameplay size. For ordinary grounded humanoids, `body_scale_cv <= 0.08`, normalized `anchor_y_std <= 0.05` and profile scale drift around `0.08` are useful diagnostics. These are not universal for crouching, flight, creatures changing posture or FX. Do not hide model drift with per-frame resizing.

`--allow-source-edge-touch` permits only a visually reviewed complete raw contour; it never permits clipped output or paste clamping. Regenerate a clipped snout, tail or weapon. Read all runtime metadata before integration; successful alpha/geometry checks do not prove animation quality.

## Registered animation clips

For already extracted common-canvas RGBA PNGs, do not normalize again:

```json
{
  "schema": "generate2dsprite.animation_clips.v1",
  "frames": ["run-0.png", "run-1.png", "idle.png"],
  "anchor_px": [48, 86],
  "clips": {
    "run": {"frames": [0, 1], "duration_ms": [90, 110], "loop": true},
    "idle": {"frames": [2], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "run", "idle": "idle"}
}
```

```bash
python scripts/build_animation_clips.py --manifest clips.json --output-dir output/hero-clips
```

This validates same-size still 8-bit RGBA PNGs, explicit durations and one shared root, copies source PNG bytes and creates lossless WebP previews with verified decoded timing. It does not implement a game state machine or approve gait. See [character-animation.md](character-animation.md).

## Whole-frame packaging

```bash
python scripts/assemble_frames.py --input phase-0.png phase-1.png phase-2.png phase-3.png \
  --duration 250 --output-dir output/full-frame-loop
```

Or use `--sheet source.png --rows 2 --cols 2`. Uneven measured layouts can use `--crop-boxes boxes.json`; inspect `--help` for schema. No keying, resize, silhouette crop, alignment or continuity repair occurs. Complete backgrounds remain rectangular. Equal-frame size and native PNG mode are verified; diagnostics are not evidence of a seamless loop. Detailed scene planning belongs to the map skill.

## Godot Sprite3D (optional)

`process --godot-world-height 0.70` writes `godot-sprite3d.json` with a pixel size derived from reference subject height, source origin converted to Sprite3D offset, frame names and timing. Later actions using the reference profile reuse that pixel size and world height, preserving legitimate crouching/recoil.

```bash
python scripts/generate2dsprite.py build-godot-bundle \
  --action idle=output/idle/godot-sprite3d.json \
  --action attack=output/attack/godot-sprite3d.json \
  --default-action idle --one-shot attack --output output/godot-bundle.json
```

The bundle rejects incompatible world height or pixel size. Do not compensate for wrong action scale with per-action runtime magnification. Other engines can consume PNGs/clips and the declared origin directly; no Godot requirement applies to this skill.
