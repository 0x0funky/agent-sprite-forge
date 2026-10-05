# Character animation for a playable 2D scene

Use this reference when a character needs convincing movement at gameplay size, explicit clips, or a state-driven side-view animation set. The gait examples below target a biped platformer; retain the project's camera and anatomy for top-down actors, quadrupeds, or flying characters.

## Design a readable character before expanding its animation

Accept one master at the intended game scale. Choose recognizable silhouette, head/body proportions, costume color groups, and one or two identity details. Use a limited, consistent pixel-cluster vocabulary: a decorative mark that disappears at gameplay size should not drive nine separately redrawn microtextures. Record the logical design grid, measured source dimensions, intended displayed body height, palette, light direction, view, and facing.

A prompt such as "64x64 pixel character" describes an intended design scale; it does not prove the returned image has that pixel geometry. Inspect the actual output. For strict pixel art, preserve the accepted logical grid and use nearest-neighbor, preferably integer display scaling. If an explicit reduction to a logical grid is needed, apply one shared reduction contract and inspect the result; do not independently resize each pose to a bounding box or claim that a high-resolution pixel-like painting is automatically a clean low-resolution sprite.

Use the same accepted master in every generation through the tool's actual reference mechanism. State identity and camera invariants separately from allowed pose changes. A pose/layout guide supplies geometry only, not final artwork. Keep wide slash, dust, muzzle flash, and impact effects separate from the body asset.

## Choose poses and clips before choosing sheet dimensions

For a new run/walk plan, **8–12 useful poses per action or direction** is a practical starting range unless deliberately sparse animation is requested. Retain valid four-frame source compatibility and intentional limited animation. A low count is a prompt to inspect contact/passing coverage, weight transfer and the loop seam, not proof of bad motion; a high count is not proof of good motion. Do not pad an incomplete cycle with copies. The legacy CLI presets still contain four poses and emit an advisory unless `--intentional-low-frame-count` records that choice.

`3x3` is a packing choice, not a smoothness setting. Nine nearly identical poses can still look stiff; four well-spaced poses may suit a deliberately restrained style. Additional useful phases need enough per-cell pixels and clear identity. Choose a sheet for economical coherent phases, or individual full-canvas frames when each pose needs more detail; independent calls must keep the same master and registration contract.

An optional small locomotion prototype can place **eight run phases plus one neutral idle** in nine cells. This is a narrow same-character locomotion exception, not permission to generate an entire unrelated attack/hurt/death bundle in one atlas. If a run-only sheet is more reliable, generate it separately and add the accepted idle during final packing. Do not force a ninth run phase or duplicate the first frame merely to fill a square.

Example row-major, zero-based plan for a side-view biped:

| Index | Pose purpose | Contact and motion |
|---|---|---|
| 0 | Left contact | Left leg leads into the contact; right arm opposes it. |
| 1 | Left down/compression | Weight settles over the support leg; knees compress. |
| 2 | Left passing/push-off | Free leg passes under the body; support foot prepares to leave. |
| 3 | Up/flight toward right contact | Body rises, limbs open toward the next lead; feet may clear the ground. |
| 4 | Right contact | Opposite lead and arm; preserve clothing/weapon asymmetry. |
| 5 | Right down/compression | Opposite support, consistent anatomical scale. |
| 6 | Right passing/push-off | Return swing and push-off; do not repeat frame 2 unchanged. |
| 7 | Up/flight toward left contact | Continue into frame 0 without a second idle/hold at the seam. |
| 8 | Neutral idle | Stable readable stance; this frame is excluded from the run loop. |

Adapt this choreography to actual gait and speed. A walk retains support differently from a run; do not add airborne frames to every walking creature. These phase names are an animation brief, not proof that the model produced correct biomechanics.

### Compact prompt contract

```text
Use the supplied accepted character as the exact identity, palette, anatomy, camera and pixel-design reference. Create a 3x3 equal-cell sheet in row-major order: the eight specified run phases, then one neutral idle. The run playback is cells 0–7 only; cell 8 is a separate idle.

Keep the same cell canvas, camera distance, standing-equivalent anatomical scale, costume, handedness and facing. Use one fixed registration origin relative to the cell. Animate the torso/pelvis and limbs around that origin: modest down/up motion and airborne clearance are intentional. Only contact phases need the support foot on the shared ground reference. Do not pin every lowest pixel to that line or make every pose the same bounding box.

Give each phase a visibly different leg/arm silhouette and coherent weight transfer. <Insert the explicit phase list.> Keep clear limb separation at gameplay scale; avoid extra fingers, accessories, detached shadows, dust and weapon effects. The complete motion envelope fits every cell with margin. Background: <verified transparency or uniform chroma contract>. No labels, numbers, grid lines or checkerboard painting.
```

For the first sheet, reserve roughly 15% clear padding on each side of every cell around the **whole action envelope**, including tail, ears, limbs and held equipment. Reduce one shared anatomical scale if the envelope is too large; never shrink only the widest pose. Treat this as a containment starting point and verify the measured result. Source-level tails or boots can cross an invisible nominal grid boundary even when the sheet looks orderly; inspect the actual extracted frames before accepting the grid. See [prompt-rules.md](prompt-rules.md) for recovery.

## Keep scale, registration, contacts, and physics distinct

- **Cell canvas:** the same rectangle for every source frame; reserve the union of the whole action's motion envelope.
- **Registration/root anchor:** one source-pixel coordinate attached to the runtime actor. It need not lie on an opaque pixel. A fixed root does not mean the pelvis must be painted at the same Y in every frame.
- **Pose/contact landmarks:** per-frame annotations such as planted toe, pelvis, or head. They help review gait and foot slide; they are not automatically runtime anchors.
- **Collider:** authored game geometry. Do not replace it every frame with the artwork's alpha bounds.

`generate2dsprite.py process --scale-strategy preserve` prevents per-pose magnification, but its feet/bottom/center alignment still translates each detected subject. That is useful for genuinely grounded alignment repair; it can erase intentional run flight, breathing bob, recoil, or jump motion. For already registered animation, retain fixed rectangular cells and apply at most one common crop/padding/scale transform to the entire set. Do not invent an unsupported `--align none` switch. Keying or alpha cleanup must likewise preserve each cell's coordinate system; inspect cleanup separately from alignment.

When trimming for an engine atlas, retain each frame's trim offset and original canvas size so the same root is reconstructed. If original root `R` and crop origin `C` share source coordinates, the trimmed-frame root is `R-C`; do not replace it with the trimmed bottom center. If scale `s` is explicit, transform both consistently. Atlas placement coordinates are not character origins.

For a reported floating-foot or uneven-baseline defect, inspect the **exported game-size frames** against the runtime root, not just the raw sheet. Bounding-box bottoms are commonly exclusive: a last visible pixel on row 60 has its lower edge at 61. Confusing the row index with the pixel edge produces a one-pixel sink or gap. Separate solid-foot bounds from faint alpha noise, and annotate grounded, toe-contact and flight phases by inspecting the artwork; alpha bounds alone cannot classify a gait.

If that review reveals residual registration errors, preserve the accepted artwork and apply small, explicit per-frame integer translations on the unchanged canvas. This is a targeted registration repair, not automatic per-pose bottom fitting: record old/new bounds, offsets and intended contact clearances, retain the originals, and verify that no nontransparent pixels are clipped or resampled. Check head/waist landmarks and opposite-half phases before applying a single shift to an entire atlas row; a row's idle pose may not establish the correct baseline for its run poses. Compare before/after at identical phase and timing with a visible ground line. Preserve intentional flight and compression instead of forcing every frame to touch the floor.

## Timing, stride, and movement states

Define explicit frame indices, per-frame durations, loop behavior, and state-to-clip mapping. Do not play all atlas cells with `time % 9`, and do not ping-pong a run cycle to hide an incomplete return phase. A single idle still is acceptable for the prototype but should be described as a still rather than a breathing loop.

For an in-place run with cycle duration `T` seconds and intended travel distance per cycle `L`, the reference movement speed is `L/T`. If gameplay speed changes, choose a bounded playback multiplier from `abs(actual_speed)/(L/T)`, or advance gait phase by grounded distance traveled. Inspect planted-foot motion in world coordinates to tune stride. These are presentation controls: do not let image timing alter collision simulation, and do not continue cycling a run because the input key is held while the actor is blocked by a wall.

For a platformer, select at least meaningful **idle, run, rise, apex, fall, and land** states as required by the task. Runtime velocity/ground contact chooses airborne states; a timer alone must not land the character before collision does. Rise/apex/fall can begin as separately authored still poses; land may be a short one-shot recovery. If those assets have not been generated, record the temporary pose reuse instead of claiming a complete jump set. Preserve run phase while remaining in run; restart one-shot land only on an actual airborne-to-ground transition. Facing flips are acceptable only if the accepted design tolerates mirroring handed equipment or text.

### Explicit clip manifest

Use `scripts/build_animation_clips.py` for already prepared common-canvas frames. This example uses measured 96x96 cells with the runtime origin at `[48,86]`; replace paths, origin, timings, and stride for the actual artwork. Paths are relative to the manifest directory. The illustrative 80-unit stride and 640 ms run cycle imply 125 world units/second, not a required game speed.

```json
{
  "frames": [
    "frames/run-0.png", "frames/run-1.png", "frames/run-2.png", "frames/run-3.png",
    "frames/run-4.png", "frames/run-5.png", "frames/run-6.png", "frames/run-7.png",
    "frames/idle.png"
  ],
  "anchor_px": [48, 86],
  "clips": {
    "run": {"frames": [0,1,2,3,4,5,6,7], "duration_ms": 80, "loop": true, "stride_world_units": 80},
    "idle": {"frames": [8], "duration_ms": 400, "loop": true}
  },
  "states": {"moving": "run", "idle": "idle"}
}
```

```bash
python skills/generate2dsprite/scripts/build_animation_clips.py \
  --manifest output/hero/clips.json --output-dir output/hero/compiled-clips
```

This manifest contains only idle/run. Add actual clips and mappings when rise/apex/fall/land artwork exists; their names are not magic engine transitions. `duration_ms` may also be a positive-integer list matching the clip's sequence length for intentional timing accents. `loop` is an explicit boolean. Frames can use unique `{ "name": "run-contact", "file": "frames/run-0.png" }` entries, and clips may reference unique names instead of zero-based indices; string-path frame names default to their filename stems and must also be unique.

The builder requires still 8-bit RGBA PNGs, all with the same canvas and each containing both fully transparent and visible pixels. Prepare alpha beforehand without changing registration; the builder does not key, crop, resize, normalize, or fix anchors. It accepts one finite shared `anchor_px` inside/on the canvas and rejects per-frame anchor overrides. This describes the intended runtime root, not a measured anatomical feature.

Use a new output directory. Publication is staged and refuses existing output. Deliverables are byte-preserved `frames/frame-00.png` and subsequent frames, `animation-clips.json`, `source-manifest.json`, a native-scale annotated `contact-sheet.png`, and clip previews such as `clips/clip-00.webp` (use each clip's `preview.file` mapping). Root markers appear only on the QA contact sheet. Preview verification checks decoded timing intervals, alpha, and visible RGB, including merged duplicate frames; one-frame clips retain a timed animation container. WebP loop 0 means infinite and 1 means one play. The optional stride is user-authored; exported nominal travel speed is a calculation, not measured foot contact.

Duplicate-frame and wrap diagnostics do not establish gait quality, physical root stability, or a good visual seam. The builder is an asset packager, not a controller or animation-quality judge.

## Review what the player will see

For longer **constant-rate** registered RGBA frame sequences, the local [animation review helper](../../video2dsprite/scripts/animation_review.py) can build a light/dark, frame-step and interval-selection page: `python skills/video2dsprite/scripts/animation_review.py review --frames-dir output/hero/frames --out-dir output/hero/review --fps 12.5`. Supply the actual source rate; it does not read variable per-frame durations from a clip manifest. Its reduced previews preserve canvas registration, and explicit `cut --selection ...` exports original PNG bytes after validating hashes. Candidate recurrence must improve on ordinary adjacent-frame change; half-cycles and identity errors can still pass that heuristic. Use the whole authored clip when no convincing repeated cycle exists, and use the clip manifest's preview for variable-duration timing.

### Diagnose perceived stutter before requesting more frames

Separate four clocks: simulation updates, display/render cadence, sprite pose changes, and exported preview/video sampling. A 60 Hz display does not make eight drawings into sixty drawings, and a low-frame-rate recording can exaggerate uneven pose holds. Record actual render intervals and pose indices for a short representative run; a clean automated browser trace does not establish performance on the user's device.

Compare the runtime, gallery, and exported clip at the same intended movement speed. A gallery must use the manifest's duration sequence and an explicit playback multiplier, rather than a separate hard-coded FPS. For distance-driven motion, its cycle time is `stride / abs(actual_speed)`; the manifest's nominal time can legitimately differ when this multiplier is intentional. A/B previews should label their timing and source differences. If comparing continuous motion, capture at the intended display cadence where practical (for example 60 fps), and report the encoded cadence rather than inferring it from a filename.

Inspect state boundaries as well as the loop. A held landing pose while the collider keeps translating creates visible slide of approximately `speed * hold_duration`. For a moving landing, consider a brief recovery over the continuing gait; reserve a held crouch for a stopped landing. Preserve useful locomotion phase across short transitions when the pose contract allows it. Do not alter jump physics just to conceal a missing pose.

Rank large adjacent-frame changes, including last-to-first, to locate review candidates; do not use pixel-difference scores, unique-image hashes, or frame count as a quality score. Inspect head/costume drift separately from intentional limb movement. If only one transition is bad, repair the involved key poses or add controlled in-betweens while retaining the accepted frames and registration. Repeatedly generating larger atlases can multiply identity errors.

### Optional cutout or hybrid route

When continuous movement and stable character identity matter more than a strict hand-drawn pixel grid, a small generated part kit can be a useful comparison prototype: head, torso, upper/lower limbs, foot, and separate secondary accessories, all sharing the accepted master. Specify camera, joint overlap, attachment pivots, facing and draw order. Inspect the assembled rest pose before expanding clips; plausible isolated parts do not guarantee coherent anatomy when combined.

Animate the parts with authored key poses or a rig, and blend joint transforms at state changes. Check planted feet in world coordinates, fixed limb lengths, reachable joint targets, joint coverage, depth order, and cycle seams. AI supplies the bitmap art; the runtime controls the motion. This is not newly generated hand-drawn imagery on every display frame. Rotated low-resolution parts can shimmer or look like a puppet even with nearest-neighbor sampling, so evaluate at gameplay size and retain the original sprite route for comparison. For strict pixel work, an offline rig can instead help lay out poses which are then rasterized on the shared grid and reviewed/cleaned; more samples alone do not solve pixel consistency.

Hybrid animation can keep a stable head/body while swapping a few authored hands, feet, expressions, or action poses. Use it only where it improves the requested visual style. Do not replace an accepted pixel aesthetic with a rig merely because it is easier to produce many samples.

Play the run in place and while moving over a visible ground reference, at the actual display scale and both intended facings. Inspect contact/down/passing/up differences, planted-foot slide, deliberate bob, silhouette separation, costume consistency, transition pops, and the last-to-first seam. Step through the frames to distinguish a bad pose from a bad anchor or timing choice before regenerating.

Check empty/clipped frames, scale/registration consistency, alpha edges over light and dark backgrounds, clip indices, and durations. Those checks cannot establish appealing anatomy, convincing motion, or visual polish. Preserve raw masters/sheets and the source-to-export mapping so processing-induced damage can be corrected without rerolling good art. Never use optical-flow interpolation, blended in-betweens, or a CSS whole-body bounce as evidence of newly drawn pixel animation.

## Engine/editor notes and primary sources

Checked **2026-09-11**. The workflow recommendations above are this skill's design guidance; the sources establish storage and runtime mechanics, not a model's artistic quality.

- Aseprite separates sheet layout/import dimensions from animation frames, and can export selected tags. A matrix is a storage layout. [Sprite sheets](https://www.aseprite.org/docs/sprite-sheet/)
- Named tags group frame ranges and carry playback direction; frame duration can be edited independently. [Tags](https://www.aseprite.org/docs/tags/), [Frame Duration](https://www.aseprite.org/docs/frame-duration/)
- Aseprite slice pivots describe a base/central location. Preserve coordinate conversion when using them as runtime origins. Its Lua `Frame.duration` is in seconds, while `.aseprite` frame headers store milliseconds; convert deliberately. [Slices](https://www.aseprite.org/docs/slices/), [Frame API](https://www.aseprite.org/api/frame#frameduration), [File specification](https://github.com/aseprite/aseprite/blob/main/docs/ase-file-specs.md)
- Godot `SpriteFrames` stores per-clip speed and per-frame relative duration. Absolute seconds are `relative_duration / (animation_fps * abs(playing_speed))`; for an imported duration `ms` at playback speed 1, use `relative_duration = ms * animation_fps / 1000`. Check the target engine version's loop API when importing. [SpriteFrames](https://docs.godotengine.org/en/stable/classes/class_spriteframes.html)
- Godot documents part-based cutout animation, pivot/depth ordering, and combining it with selected cel animation. These concepts support the optional hybrid route; check version-specific APIs before implementing its tutorial steps. [Cutout animation](https://docs.godotengine.org/en/stable/tutorials/animation/cutout_animation.html)
