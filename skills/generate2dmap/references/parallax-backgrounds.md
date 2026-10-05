# Parallax, camera coverage and backdrops

Use this reference for a layered side-view scene, depth during camera movement, a single painted backdrop that has to pan, or a backdrop that must survive several screen shapes. Do not impose a side-view stack on top-down maps. Commands run from the project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs stay in the project.

## 1. Planning kit

Give every visual feature one owner layer before generating anything: which depth band it lives in, its scroll factor, and which layer shows through its openings. A useful starting stack for a side view:

| Layer | Content and alpha | Typical scroll factor |
|---|---|---|
| Sky / backplate | Opaque atmosphere only; no hills, trees or platforms owned by other layers | 0 to 0.1 |
| Far silhouette | Sparse distant hills or ruins; transparent sky openings | 0.1 to 0.3 |
| Mid silhouette | Distinct trees or architecture behind the playable lane; real transparent openings | 0.3 to 0.7 |
| Gameplay plane | Terrain, props, actors, collision (not a parallax layer) | 1 |
| Near foreground (optional) | Sparse framing foliage or arches; wide openings over the action | 1 to 1.5 |

This is an example, not a required count. A small layered delivery can be an opaque sky plus two cutout bands. Report scenery layers separately from gameplay structures and actors. One flattened scene drawn several times at different speeds is one visual source reused, not depth art.

Use atmospheric separation on purpose: quieter contrast and fewer details far away, stronger shapes near the playable plane. A continuous high-contrast bridge or ledge in a background layer can read as a standable platform; break or soften it and recheck it against the real terrain.

Prompt each cutout layer for its own content only:

```text
Create ONLY <far hills / mid forest / sparse near framing> for the supplied side-view composition. The reference fixes palette, lighting, pixel-cluster scale and horizon; it is not a request to redraw the whole scene. Include <owned silhouettes>. Exclude <the sky, other depth bands, terrain, props, actors and UI>. Keep the action corridor <location> open. Keep the full rectangular canvas and the planned anchor; no centring around the objects. Everything above and between the silhouettes is <native alpha, or one exact chroma colour for keying>; no painted checkerboard, fog rectangle or cast-shadow plate. <If repeating: one repeat axis, an uninterrupted boundary profile at that join, no cap border.>
```

Ask the sky for complete opaque art. Measure what comes back: a requested size is not proof of the returned size, and alpha is proven by the saved pixels, not by the model's name. Reject fake RGB checkerboards. Key a uniform chroma layer to RGBA with the sprite or prop tools, but keep the full canvas and anchor: never run scenery through a bbox-fit or feet-align step.

## 2. The plan and its transform

Record each layer's actual source size (measured), one uniform `scale`, `anchor_px` (source pixels), `offset` and `camera` (world pixels at zoom 1), `scroll_factor` per axis, `alpha`, render order and `repeat` axes, plus the viewport, signed camera ranges, the zoom range and the zoom pivot. Paths are relative to the plan file.

```json
{
  "schema": "generate2dmap.parallax_plan.v1",
  "viewport": [960, 540],
  "camera": {"x": [0, 1920], "y": [0, 0], "zoom": [1, 1.25], "pivot": "center"},
  "aspects": ["16:9", "19.5:9"],
  "layers": [
    {"id": "sky", "role": "sky", "image": "sky.png", "alpha": "opaque", "scale": 1.3, "offset": [-144, -81], "scroll_factor": [0, 0]},
    {"id": "far", "role": "far", "image": "far.png", "alpha": "transparent", "scale": 1.3, "offset": [0, -81], "scroll_factor": [0.2, 0], "repeat": [true, false]},
    {"id": "near", "role": "near", "image": "near.png", "alpha": "transparent", "scroll_factor": [1.25, 0], "repeat": [true, false]}
  ]
}
```

With `sky.png` 1040x480, `far.png` 1200x480 and `near.png` 1920x540 this plan passes, including the 19.5:9 screen it lists; the default sweep still warns that nothing covers a 4:3 (960x720) view, which this game does not claim.

The transform, componentwise, with the zoom pivot `P`:

```text
screenTopLeft = P + (offset - anchor_px * scale - camera * scroll_factor - P) * zoom
screenSize    = sourceSize * scale * zoom
```

`camera` is the world position at the viewport's top-left at zoom 1. A positive-factor layer moves left as the camera moves right; negative factors and negative cameras are valid. With `"pivot": "top-left"` (the default) this is the old transform.

### Pivot

`camera.pivot` is the screen point that stays fixed while zooming: `"top-left"`, `"center"`, or `[x, y]` in viewport pixels. Most engines zoom about the screen centre (a Godot `Camera2D`, a centred Phaser camera); declare `"center"` for them. Convert a centre-anchored camera position to this plan's camera with `camera = cameraCenter - viewport / 2` (at zoom 1). The validator's corner checks stay exact under any pivot, so a plan that only covered under the old top-left assumption now shows its real gap (MAP-08: a 320x180 view that passes top-left shows a 10 px gap at zoom 0.75 with a centred camera).

### How big a non-repeating layer must be

For one axis with viewport extent `W`, zoom range `[z_min, z_max]`, camera range `[c_min, c_max]`, factor `p`, displayed extent `D = sourceSize * scale`, base `b = offset - anchor_px * scale`, pivot share `f` (0 top-left, 0.5 centre), `m = min(p*c_min, p*c_max)` and `M = max(p*c_min, p*c_max)`:

```text
b     <= m + f * W * (1 - 1/z_min)
b + D >= M + f * W + (1 - f) * W / z_min
```

The minimum extent `D >= W / z_min + |p| * (c_max - c_min)` does not depend on the pivot; the pivot only moves where that extent must sit. For `z = 1`, `p >= 0` and camera travel from zero this is the familiar `W + p * C`. Apply the same check vertically.

### Repeating layers

The repeat period is the actual displayed source extent: export a crop you mean to repeat as its own full rectangle, and never pad between copies or stretch a layer to a wished-for width. At runtime choose integer copy indices whose rectangles cover the screen after the transform, on both sides; wrap negative offsets with a floor-based modulo, recompute the copy count at the smallest zoom, and draw all copies from one rounded origin plus whole periods so fractional cameras cannot open one-pixel cracks. Inspect three adjacent copies and move through a full wrap in both directions.

## 3. Validate

```bash
python "<skill-dir>/scripts/validate_parallax.py" --spec stage/parallax-plan.json --report stage/qa/parallax-qa.json
```

It prints the full result as one line of ASCII JSON and exits 0 on pass, 1 on failure. `--report` saves the same result (schema `generate2dmap.parallax_validation.v2`) and never replaces an existing file; `--strict` saves nothing when the plan fails. It checks, per layer: real alpha against the declared `alpha`, unique ids, finite transforms, exactly one opaque sky, and canvas coverage at every camera x/y and zoom corner, reporting the gap in pixels on each side.

- **Coverage default:** the sky, every repeated layer and every near or foreground role (`near`, `near_*`, `foreground`, `foreground_overlay`, ...) must cover the viewport. A layer's `require_canvas_coverage` wins (set `false` for a deliberately local near arch). `--coverage sky-only` is the old rule; `--coverage all` requires every layer. Layers that are not required but leave the viewport entirely at some camera corner produce a warning.
- **Aspect sweep:** coverage is re-checked on 16:9, 19.5:9 and 4:3 screens (`--aspects`, or `none`). With the default `expand` policy a wider or taller screen grows the viewport and keeps the pivot's share of it, so a centred camera grows both sides; `fixed-height` and `fixed-width` are the alternatives (`--aspect-policy` or the plan's `aspect_policy`). Failures warn; aspects listed in the plan's `aspects` must pass.
- **Repeat seams (MAP-14):** the wrap step (last column to first) is compared with the layer's own neighbour steps (`forge_core.seam_report`): `continuous` (step within the neighbours' p95), `seam` (a visible jump), `duplicate-edge` (an edge column repeated, a one-pixel stutter that edge-equality would call perfect) or `flat`. Seam verdicts warn; `--strict-seams` fails them. `seamless_verified` stays false: a statistic is not a proof.
- **Pixel grid:** with `"pixel_art": true`, `"sampling": "nearest"` or `--pixel-grid`, `scale * zoom` must be a whole number at both zoom extremes and the layer's rest position (camera 0) must land on whole pixels; a fractional `scroll_factor * zoom` warns that the layer moves by sub-pixels.

The checks prove geometry under this transform, not art: they cannot tell whether a cutout holds the intended objects, whether layers are in the right order, or whether the engine draws enough repeat copies. Rectangular canvas coverage is not opaque coverage; a sky can hide a missing layer.

## 4. Sub-pixel presentation

Keep camera and collision coordinates smooth and make drawing deliberate:

- Pixel art: render at the logical resolution and scale the frame by a whole number with nearest sampling, or keep `scale * zoom` whole for every layer. Fractional zoom on pixel art gives uneven pixels; prefer whole zoom steps.
- Snap each layer's drawn position to whole screen pixels every frame (one rounding rule for every layer and copy), but keep the camera itself fractional. A slow layer then steps (a factor of 0.25 moves one pixel per four camera pixels): that is correct, not a bug.
- Do not mix snapped pixel layers with unsnapped high-resolution overlays that track the same world points: they drift by up to a pixel plus any shake. Snap both or neither.
- Painted (non-pixel) layers may draw at fractional positions with linear sampling; still draw repeat copies from one origin.

## 5. Single-plate pan

One painted backdrop can still move with the camera: zoom it slightly and pan the window with the camera's progress. With plate size `W x H`, zoom `z` (about 1.08 to 1.15), screen aspect `a`, horizontal progress `u = cameraX / (worldWidth - viewWidth)` (0..1) and a fixed vertical focus `v`:

```text
window = (W / z, H / z), then trimmed to aspect a (keep the height if wider, the width if taller)
x0 = (W - windowW) * u        y0 = (H - windowH) * v
```

The whole plate moves as one, so this suits battle, title and story backdrops rather than deep side-scrolling. Preview the pan on the composed scene (frames from `u = 0` to `1`, stacked, deterministic):

```bash
python "<skill-dir>/scripts/compose_layered_preview.py" --base stage/plate.png --placements stage/placements.json --output stage/qa/preview.png --plate-pan stage/qa/plate-pan.png --pan-viewport 960x540 --pan-zoom 1.12 --pan-frames 5
```

Then check that the subjects survive both pan ends:

```bash
python "<skill-dir>/scripts/conform_background.py" validate-crops --input stage/plate.png --zoom 1.12 --focus 0,0.5 --focus 1,0.5 --subject crest=600,200,680,280 --min-margin 8 --output-dir stage/qa/pan-crops
```

## 6. Aspect-safe backdrops

Image tools return their own sizes (a 1280x720 request came back 1672x941). Conform the painting to the game's size with a recorded transform instead of an ad-hoc resize:

```bash
python "<skill-dir>/scripts/conform_background.py" conform --input art/forest.png --size 1280x720 --mode cover --output-dir stage/bg-cover
```

`cover` takes the largest window of the target aspect (the same pixels as PIL `ImageOps.fit`, placed by `--focus`). When the painted ground must sit on the authored floor, use `ground-fit`: the smallest uniform zoom that maps the painted ground row (`--ground-y`, source pixels) onto the floor row (`--floor-y`, output pixels) with no padding:

```bash
python "<skill-dir>/scripts/conform_background.py" conform --input art/forest.png --size 1280x720 --mode ground-fit --ground-y 718.7 --floor-y 610 --subject well=700,500,820,700 --aspects 16:9,19.5:9,4:3 --output-dir stage/bg-fit
```

`conform.json` records `scale`, `src_rect` and the map `out = (src - src_rect[0:2]) * scale` (inverse `src = out / scale + src_rect[0:2]`); move placements authored on the old image through it rather than eyeballing. Subject boxes are mapped too and must stay inside the output and, with `--aspects`, inside each aspect's cover crop; `--strict` publishes nothing when one is cut. To keep a backdrop usable from 16:9 to 19.5:9, bake it with spare height (3:2 works) and choose a vertical focus that keeps crests and faces inside the 19.5:9 crop; prove it with `validate-crops` on the plate the game loads.

## 7. Runtime acceptance

- Toggle each layer alone and confirm it differs as intended; view transparency over light and dark backgrounds.
- Freeze actor and time, move the camera between known coordinates and check that independently identifiable landmarks shift by the declared factors.
- Visit camera extremes, reversals, vertical bounds, zoom extremes, repeat boundaries and the target screen aspects; add the shake or overscan envelope if the game uses one.
- Render at the real logical resolution; for strict pixel art use whole-number scaling with nearest sampling.
- Check actor and prop contrast, landings and foreground obstruction while moving. Keep visual coverage, mathematical coverage and playability as separate findings.

Primary sources (checked 2026-09-11 for the improved fork's version of this guide): Godot's [2D parallax tutorial](https://docs.godotengine.org/en/stable/tutorials/2d/2d_parallax.html) covers scroll scale, repeat sizing and the extra coverage zooming out needs; the [Parallax2D class](https://docs.godotengine.org/en/stable/classes/class_parallax2d.html) exposes `scroll_scale`, `repeat_size` and `repeat_times`. Do not let a manual camera transform and automatic camera following apply the same displacement twice.
