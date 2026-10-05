# Parallax and camera coverage

Assign one owner per visual feature: opaque sky, far silhouettes, middle landmarks, near scenery and optional foreground. Use only useful layers, with real alpha openings that reveal earlier layers. Reusing the same whole painting at different speeds is not independent depth art.

Use an accepted master for horizon/palette/orientation; ask each layer for only its content. Preserve full canvas/anchor, not bbox crops. Measure outputs and declare uniform scale. Different source sizes work with explicit transforms; automatic stretch distorts the horizon.

```json
{
  "viewport": [960,540],
  "camera": {"x": [0,1920], "y": [0,0], "zoom": [1,1.25]},
  "layers": [
    {"id":"sky", "role":"sky", "image":"sky.png", "alpha":"opaque",
     "scale":1, "offset":[0,0], "anchor_px":[0,0], "scroll_factor":[0,0], "repeat":[false,false]},
    {"id":"far", "role":"far", "image":"far.png", "alpha":"transparent",
     "scale":1, "offset":[0,0], "anchor_px":[0,0], "scroll_factor":[0.2,0],
     "repeat":[true,false], "require_canvas_coverage":true}
  ]
}
```

Use actual assets sufficient for the viewport/camera interval. Paths are plan-relative. This plan's exact transform is:

```text
screenTopLeft = (offset - anchor_px * scale - camera * scroll_factor) * zoom
screenSize = sourceSize * scale * zoom
```

Offset/camera are world pixels; anchor is source pixels. Positive uniform zoom/scale; signed factors are allowed. Adapt the plan if the runtime uses a different transform, including centered cameras.

```bash
python scripts/validate_parallax.py --spec parallax-plan.json --report parallax-qa.json
```

Checks include actual alpha, unique IDs, finite transforms, one opaque sky, extrema coverage and opposite-edge repeat diagnostics. Reports cannot overwrite source images/specs.

Limits: rectangular canvas coverage is not opaque-content coverage; transparent holes may be intentional. Repeat checks assume an unbounded lattice, whereas runtime needs enough finite copies at all zooms and both travel directions. Edge metrics do not certify an artistic seam. Rotation, perspective, animation, rounding/culling and alternate camera transforms are not simulated.

Traverse the real camera: confirm distinct landmark motion, no gaps/duplicates and readable action space. A valid sky or one screenshot cannot prove near-layer alpha/parallax. For in-layer animation, preserve dimensions/transform and avoid model-generated camera panning; use registered local video plus poster where useful.
