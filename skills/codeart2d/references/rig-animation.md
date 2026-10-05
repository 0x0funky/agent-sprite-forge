# Rig animation: skeletal code-art characters

Use `scripts/rig_animate.py` when a small character (visible height up to 48 px by default, 49-64 px with the user's consent) should move through several clips with a fixed root, exact palette and feet that do not slide. Claude writes two text files, a rig (portable SVG) and an animation (`codeart2d.rig_anim.v1` JSON); the script poses, renders, checks and packages the frames. Everything is code-drawn, with no image model: say so whenever you deliver the frames.

Painterly, identity-rich or large characters belong to the image route. Rig animation still looks like a cut-out puppet; it is precise, editable and repeatable, not hand-drawn.

The worked example is `examples/hero.rig.svg` with `examples/hero.anim.json`: a 64x64 hero (about 48 px visible) with an 8-frame walk and a 4-frame idle.

## Commands

Run from the project root; outputs go to a new folder inside the project.

```text
python "<skill-dir>/scripts/rig_animate.py" --rig hero.rig.svg --anim hero.anim.json --output-dir out/hero-v1 --route pixel --build-clips --strict-qc
python "<skill-dir>/scripts/rig_animate.py" --anim hero.anim.json --output-dir out/hero-hd --route vector --zoom 4 --stroke-scale 1.0 --build-clips
python "<skill-dir>/scripts/rig_animate.py" --anim hero.anim.json --output-dir out/hero-idle --clips idle --palette palette.json --variant night
python "<skill-dir>/scripts/rig_animate.py" --anim hero.anim.json --output-dir out/hero-godot --godot-world-height 1.8
```

In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. `--rig` defaults to the `rig` path inside the animation (relative to the animation file). On success the script prints one JSON line with `output`, `metadata` (codeart-meta.json), `report` (rig-report.json), the clips and the frame count. Errors print `error: ...` and exit 1 (an unexpected failure too, as one `error: internal error (...)` line); a wrong argument is a usage error with exit 2, and `--help` works even before numpy is installed. JSON may be saved with a UTF-8 BOM. `--output-dir` must not exist; the work is staged beside it and published only after QA, so with `--strict-qc` a failed check leaves nothing behind.

| Flag | Meaning |
|---|---|
| `--route pixel` (default) | Pixel finishing route D: exact palette, 0/255 alpha, 1 px outline, gated per frame. |
| `--route vector` | The flattened SVG at an integer `--zoom` (default 4): anti-aliased, authored strokes. |
| `--clips walk,idle` | Render only these clips, in this order. |
| `--outline COLOR\|auto\|none` | Pixel outline and inner-line colour. `auto` uses the rig's `data-outline`, else the most common slot stroke. |
| `--outline-mode solid\|selout` | `selout` takes each ramp's `out` colour (needs `pixel.ramps`). |
| `--ss 8`, `--coverage 0.5` | Pixel route: a slot covers a pixel when at least half of its 8x8 block is inside. |
| `--stroke-scale F` | Vector route: multiply stroke widths (thicker lines read better at small sizes, e.g. 1.8 at 64 px). |
| `--palette FILE`, `--variant NAME` | Palette for `c-NAME` classes and the palette gate; a named variant recolours the rig. |
| `--margin 1` | Minimum transparent margin in rig pixels. |
| `--seam-range 0.8,1.25` | Allowed loop seam over median step (see QA). |
| `--build-clips` | Run the sibling `generate2dsprite/scripts/build_animation_clips.py` by path into `compiled-clips/`. |
| `--clips-schema v1\|v2` | Schema id written into clips.json: v2 by default, so events reach the compiled `events_ms`; v1 for a build_animation_clips that predates the v2 reader. |
| `--godot-world-height H` | Also write `godot/<clip>.sprite3d.json` and `godot/sprite3d-bundle.json`. |
| `--backend auto\|resvg_py\|resvg_js_cli\|chrome` | SVG rasterizer (resvg-py first). |

Never send these frames through `generate2dsprite.py process`: its fit and alignment re-scale and move registered code art. Hand `clips.json` to `build_animation_clips.py` (or use `--build-clips`).

## The rig (portable SVG)

```xml
<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"
     data-codeart="rig.v1" data-anchor="32 59" data-facing="right" data-outline="#1a1c2c">
  <style>.c-out{stroke:#1a1c2c;stroke-width:1} .c-pants{fill:#4a5a85} .c-boot{fill:#5a3a2a}</style>
  <g id="hips" data-pivot="32 38">
    <g id="thigh_f" data-pivot="33 38" data-z="50">
      <rect class="c-pants c-out" x="30.25" y="36.5" width="5.5" height="12" rx="2.5"/>
      <g id="shin_f" data-pivot="33 47.5">
        <rect class="c-pants c-out" x="30.75" y="46.75" width="4.5" height="11.25" rx="2"/>
        <g id="foot_f" data-pivot="33 57" data-z="51">
          <rect class="c-boot c-out" x="30.5" y="55.5" width="8" height="3.5" rx="1.25"/>
        </g>
      </g>
    </g>
  </g>
</svg>
```

- **Root.** `viewBox="0 0 W H"` equal to `width`/`height`: one unit is one logical pixel. `data-anchor="X Y"` is the root on the ground line and becomes `anchor_px`. `data-ground` overrides the ground line (default: the anchor's y). `data-facing` (`right` or `left`) names the toe side for the contact report. `data-outline` is the default outline colour.
- **Bones.** `<g id data-pivot="X Y">`, nested parent to child. Pivots are in canvas pixels of the rest pose. A bone never has a `transform` attribute: rest geometry lives in coordinates and motion in tracks. Bone ids must be unique.
- **Slots.** `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon` and `path` inside bones. `data-z` sets the draw order: it is inherited from enclosing groups and defaults to 0; ties keep document order. Draw order is independent of the hierarchy, so a front arm (z 60) can cover a front leg (z 50) while both belong to different branches.
- **Plain groups** (`<g>` without `data-pivot`) may hold slots and a `transform`; a plain group that contains bones may not have a transform.
- **Variants.** `data-variant="fist"` on a slot or a plain group inside a bone makes it one variant of that bone (hand shapes, mouths, weapons). The first variant in document order is the default; a `swap` track picks another.
- **Paint.** Prefer palette classes (`class="c-skin"`) with literal-hex rules; `var(--name)` and palette files are compiled by `codeart_core.compile_svg`. Selectors must be simple tag or class selectors (`.c-out`, `rect.c-red`); `#id` and descendant selectors are refused because slots are flattened out of their groups.
- **Refused:** NaN, infinity or complex numbers anywhere (`61.13-0.00j`), duplicate ids, CSS transforms, `transform-origin`, `<text>`, `<image>`, nested `<svg>`, and everything else outside the portable profile (see svg-profile.md). The pixel route also refuses gradients, masks, filters and opacity below 1.

## The animation (codeart2d.rig_anim.v1)

```json
{"schema": "codeart2d.rig_anim.v1", "rig": "hero.rig.svg",
 "states": {"idle": "idle", "moving": "walk"}, "entry_reference": "idle",
 "clips": {"walk": {"frames": 8, "loop": true, "duration_ms": 100, "ease": "linear",
   "stride_world_units": 24, "events": [{"t": 0, "name": "step_r"}, {"t": 0.5, "name": "step_l"}],
   "tracks": {"arm_f": {"rotate": [[0, 24], [0.5, -24], [1, 24]], "ease": "sine"}},
   "ik": {"leg_f": {"bones": ["thigh_f", "shin_f"], "end": "foot_f", "pole": [1, 0],
                    "gait": {"phase": 0.0625, "stance": 0.625, "lift": 3, "roll": 15, "x": 33.5}}}}}}
```

Clip fields:

| Field | Meaning |
|---|---|
| `frames`, `loop`, `duration_ms` | Frame count; loop or one-shot; integer ms per frame (one number or one per frame). |
| `ease` | Default easing between keyframes (default `linear`); a track's own `ease` overrides it. |
| `tracks` | Per bone: `rotate` (degrees about the pivot, clockwise on screen), `translate` ([dx, dy]), `scale` (number or [sx, sy] about the pivot), `show` (true/false) and `swap` (variant name). Each is a list of `[t, value]` with t in [0, 1]. `show` and `swap` hold their value until the next key. |
| `stride_world_units` | Travel per loop cycle in rig pixels (zoom-scaled in the outputs). Needed by gaits. |
| `events` | `{t, name, data?}` with t in [0, 1] and a shared event name (`step_l`, `hit`, `custom:...`). In clips.json each event goes to the frame nearest its time. |
| `ik` | Named two-bone IK chains (below). |
| `grounded` | Default true: every frame of a clip without IK must touch the ground line; false for jumps and airborne poses. |
| `entry_frame` | Override the computed entry frame. |
| `transitions`, `role` | Passed through to clips.json (`[{to, entry_frame?, dissolve_ms?, mode?}]`, `player`/`enemy`/`npc`/`fx`/`prop`). |

Top level: `states` maps state names to clips (written to clips.json); `entry_reference` names the clip whose first frame other clips are entered from (default `idle` when present, else the rest pose); `pixel` holds pixel-route options: `ramps` (per class like `c-tunic` or per fill colour: `{hi, mid, lo, dark, out}` or five colours) for run-length form shading toward `light` (default [-1, -1], top left), `inner_lines` (default true), `outline` and `outline_mode`.

### Easing

`linear`, `sine` (in-out), `in` (quadratic), `out` (quadratic), `step` (hold, then jump at the next key), `cubic-bezier(x1, y1, x2, y2)` with CSS semantics (x1 and x2 in [0, 1]; y may overshoot) and `spring(k, d)`: a unit-mass spring with stiffness k and damping d released from 0 toward 1. The keyframe span covers the time the oscillation needs to settle to 0.1%, and the end is pinned to exactly 1. `d*d < 4k` overshoots; larger damping approaches monotonically.

### Sampling and composition

A loop of n frames samples t = i/n, so the last frame never repeats the first; a one-shot samples t = i/(n-1), ending exactly on its last key. Every bone composes `world = parent * T(translate) * T(pivot) * R(rotate) * S(scale) * T(-pivot)`. Each slot is then emitted once per frame inside its bone's world matrix (six decimals) together with attribute copies of its ancestor groups, so classes, inheritance and clip paths keep working after flattening. Identical frames are stored once and reused by name in clips.json.

## IK and the ground constraint

```json
"ik": {"leg_f": {"bones": ["thigh_f", "shin_f"], "end": "foot_f", "pole": [1, 0],
                 "target": [[0, [35, 60]]], "angle": [[0, 0]], "ground": true}}
```

- `bones` is `[root, middle]`; the middle bone must be a direct child of the root. `end` is a direct child bone of the middle (its pivot is the ankle) or a rest point `[x, y]`. Bone lengths come from the rest pivots.
- `pole` is the direction the middle joint bends toward (`[1, 0]`: knee toward +x). Without it the rest pose must already be bent.
- Exactly one of `target` (keyframes of the ankle in canvas pixels, eased with the chain's `ease`) or `gait` (below). `angle` keys the end bone's world angle (0 = its rest orientation).
- IK replaces the rotations of its chain, so those bones may not have `rotate` tracks; the middle and end bones may not be translated, and nothing above the chain may be scaled.
- **Ground constraint** (`ground`, default true): the target is lifted until no point of the end bone's slots, rotated to the foot angle, plus the outline allowance, is below the ground line. The allowance is 1 px in the pixel route (the outline ring lies outside the fill) and half the stroke width in the vector route. To plant a foot, give a target at or below the ground; the constraint puts the outline exactly on it. The extremes are computed exactly for rectangles with rounded corners, ellipses, arcs and Bezier curves.
- **Clamp.** A target farther than both bones can reach (or too close) is clamped to the reachable ring and reported in `ik_clamps`; the `ik_clamp` check then fails. Fix the target, the hip height or the bone lengths.

### Gaits: walks that do not slide

```json
"gait": {"phase": 0.0625, "stance": 0.625, "lift": 3, "roll": 15, "x": 33.5}
```

For a loop with `stride_world_units` S, the foot is planted for `stance` of the cycle and slides back by `S * stance` relative to the body while planted, then swings forward with an eased path that rises by `lift` px and rolls the toe by `roll` degrees. Because the planted foot moves back exactly as fast as the character travels, its world position (canvas x + S * t) is constant: the contact report's drift is 0 px. Give the second leg `phase + 0.5`.

Choose numbers that keep the leg reachable: with leg length L (thigh + shin), hip height h above the planted ankle and half step e = S * stance / 2, the widest stance needs `sqrt(h*h + e*e) < L`. Lower the hips at the widest stance and raise them at the passing pose (the hero uses hips 40/39/38/39 px) and keep a small knee bend everywhere: the vector route plants the ankle 0.5 px lower than the pixel route, so a leg that is exactly straight in one route clamps in the other.

For pixel-perfect planting, make the travel per frame a whole number of pixels (S / frames, e.g. 24 / 8 = 3 px) and keep the planted foot flat (the gait does): the boot's pixels then repeat exactly, shifted by the travel.

### Contact data

`rig-report.json` records, per clip and frame and IK chain: the ankle, the target after the ground constraint, the foot angle, the lowest point, toe and heel x, `planted` (the outline sits exactly on the ground) and any clamp. Per chain it lists the planted frames, the runs of consecutive planted frames (cyclic for loops) and their world drift in x (ankle x + stride * t) and y. These numbers come from the rig itself, which an image route cannot provide.

## Routes

**Pixel (route D).** For every visible slot the script renders its fill (white, stroke dropped; a stroke-only slot renders its stroke) at 8x over a pixel-aligned box, then `codeart_core.pixel_finish` runs coverage, labels (front-most slot wins), cleanup of specks and holes, optional ramp shading, pixel-perfect inner lines where a slot meets a lower slot of another bone, and the 1 px exterior outline last. Without an outline colour neither the outline nor inner lines are drawn. Output pixels come only from slot fills, ramps and the outline colour.

**Vector.** The flattened SVG with ids prefixed per frame (`walk-03_clip`), rendered at `--zoom`. Strokes are the authored ones (times `--stroke-scale`); edges are anti-aliased. Use it for HD sprites, previews and UI-scale renders. Re-render at the target size rather than resizing a big render down.

## Outputs

| Path | Content |
|---|---|
| `frames/<clip>-NN.png` | 8-bit straight-alpha RGBA, RGB zeroed under alpha 0; identical poses stored once. |
| `clips.json` | build_animation_clips input (sprite `clips_input`): named frames, one shared `anchor_px`, per clip `duration_ms`, `loop`, `loop_policy`, `entry_frame`, `stride_world_units`, `stride_px_per_frame`, `events`; `states`, `sampling`, `pixel_art`, `art_source: code`, `body_height_px`. |
| `rig-report.json` | `codeart2d.rig_report.v1`: per-frame QA metrics, ground edge and gap, margin, contacts; per clip the seam report, contact drift, entry frame, stride and events; IK clamps, ground and margin failures; the QA envelope. |
| `codeart-meta.json` | `codeart2d.codeart_meta.v1`: `art_source: code`, the disclosure, `spec_sha256` (the animation), renderer and versions, palette, outputs, and the QA envelope over every frame. |
| `review/<clip>.png` | Review sheet at 1/2/4x (pixel) on light, dark and checker, an onion row with the anchor, palette swatches and per-frame QA lines. |
| `review/<clip>-onion.png`, `review/<clip>-travel.png` | The whole cycle overlaid; frames laid out at their world travel with planted ankles marked (planted feet must coincide). |
| `compiled-clips/` | build_animation_clips output (`--build-clips`). |
| `godot/` | `generate2dsprite.godot_sprite3d.v1` per clip plus a bundle (`--godot-world-height`). |

Entry frame: the frame of each clip closest (premultiplied mean difference) to the first frame of the `entry_reference` clip, so a walk starts from the pose nearest the idle.

## QA checks

| Check | Passes when | Fix |
|---|---|---|
| `partial_alpha`, `off_palette`, `outline_gaps` | 0 in every pixel-route frame | Opaque palette colours only; keep a margin so the outline is not clipped. |
| `l_corners` | at most 10 per frame | Smoother silhouettes: avoid 1 px notches and stair steps. |
| `seam:<clip>` | loop seam / median adjacent step in [0.8, 1.25] (loops of 3+ frames) | Make every step about equally large: uniform keyframe spacing, no held pair next to a big jump. A fully static loop counts as seamless. |
| `ik_foot_drift` | 0 px for every planted run | Use a gait or keep planted targets fixed in the world. |
| `ik_clamp` | no clamped frame | Reachable targets; see the gait reach rule. |
| `ground_row` | grounded frames end exactly on the ground line (planted IK frames; every frame of a non-IK clip unless `grounded: false`) | IK targets on the ground, or rest geometry whose outline edge is on the ground. |
| `margins` | every visible pixel at least `--margin` px from the canvas edge | A bigger canvas or a smaller envelope. |
| `build_clips` | build_animation_clips exits 0 | Read its message in rig-report.json. |

Not proven: appeal, anatomy, acting and timing in motion (look at the review sheets and play the clips), and pixel equality of planted feet when the travel per frame is fractional. The vector route's anti-aliased edges are not palette-checked.

## Lints that stop the run

| Lint | Message | Fix |
|---|---|---|
| NaN, infinity, complex numbers | `NaN is not allowed`, `complex number (a Python complex leaked into the SVG)` | Write finite plain decimals, e.g. `round(float(v.real), 3)` in generator code. |
| Clip id collisions | `duplicate id 'c'` | Unique ids; frames are id-prefixed anyway so batched pages never share a clip. |
| Unsupported structure | bone transform, transformed group holding bones, `#id` selector, nested svg | Coordinates for rest geometry, tracks for motion, classes for paint. |
| IK conflicts | `remove its rotate track`, `assumes unscaled bones`, `give pole` | IK owns its chain's rotations. |
