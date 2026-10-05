# Runtime integration: roots, clocks, transitions and pixels

How a game should play the clips that [frames-and-clips.md](frames-and-clips.md) produces. The art carries poses and a shared root; the game owns position, collision, timing and state. Poses and timing are planned in [animation-planning.md](animation-planning.md).

## Keep scale, registration, contacts and physics distinct

- **Cell canvas:** the same rectangle for every frame of a set, sized for the union of the whole action's motion.
- **Registration root (`anchor_px`):** one source-pixel coordinate attached to the runtime actor. It need not lie on an opaque pixel. A fixed root does not mean the pelvis is painted at the same height in every frame.
- **Pose and contact landmarks:** per-frame notes such as a planted toe, the pelvis or the head. They help review gait and foot slide; they are not runtime anchors.
- **Collider:** authored game geometry. Never replace it every frame with the art's alpha bounds.

`generate2dsprite.py process` alignment translates each detected subject. That repairs genuinely grounded poses but erases intentional run flight, breathing bob, recoil and jumps. For registered animation, keep fixed cells and apply at most one common crop, padding or scale to the whole set. Keying and alpha clean-up must keep each cell's coordinate system.

Source coordinates stay the truth. If the root is `R` and a trim or crop starts at `C` in the same source coordinates, the trimmed frame's root is `R - C`; never replace it with the trimmed bottom centre. Scale both together when a scale `s` is applied. A preview or mobile encode can be smaller than the source, but `anchor_px` and the frame size still describe the source pixels that world geometry uses.

To review a floating foot or an uneven baseline, check the game-size frames against the runtime root, not the raw sheet. Bounding-box bottoms are exclusive: a last visible pixel on row 60 has its lower edge at 61, and confusing the two gives a one-pixel sink or gap. Separate solid feet from faint alpha noise, and label grounded, toe-contact and flight phases by looking at the art. If small registration errors remain, keep the accepted art and apply explicit whole-pixel translations on the unchanged canvas. Record the old and new bounds, the offsets and the intended clearances, and check that nothing was clipped or resampled. Preserve intended flight and compression instead of forcing every frame to touch the floor.

## Drive the walk phase by distance travelled

A walk or run cycle should advance with the ground the actor actually covers, not with wall-clock time. Otherwise feet slide whenever speed changes, and a blocked actor runs on the spot.

- Each simulation tick, add `abs(distance moved) / stride_world_units` to a phase in `[0, 1)`. Show the frame that the clip's timeline holds at `phase * total_duration_ms`.
- When the actor is blocked by a wall, the distance is 0 and the phase freezes. Never keep cycling because the input is held.
- The authored speed is `stride_world_units * 1000 / total_duration_ms` world units per second (`nominal_travel_speed_world_units_per_second`). `speed_ref_playback_rate` compares it with the declared `speed_ref`. A time-based player at speed `v` uses the rate `abs(v) / nominal speed`; keep that rate within a sane range (about 0.5 to 2) or author another clip.
- `step_l` and `step_r` events fire when the phase passes their frame: footstep sounds and dust go there, not on a timer.

Tune the stride by watching a planted foot in world coordinates; it should not slide. Stride values in the manifest are declared, not measured from the art.

## Enter loops on the right frame

When a state starts a cycle, start at its `entry_frame` (often the first contact pose) instead of frame 0, so the first pose matches the previous state. Keep the locomotion phase across short interruptions, and across switches between clips with matching phases, such as a walk and a run that both start on NEAR contact. Restart a one-shot landing only on a real airborne-to-ground transition.

## Transitions and dissolves

`transition_hints` in `animation-clips.json` give, per source clip, the target clip, the frame to enter (`entry_frame`, `entry_ms`), an optional `dissolve_ms` and the dissolve `mode`.

- Freeze the outgoing frame at the moment of the switch and mix it with the incoming entry frame **in one draw**. `premultiplied` mixes premultiplied colour and alpha; `dither` picks one source per pixel with a 4x4 Bayer threshold, which suits pixel art.
- Never fade two overlapping sprites separately. Two source-over draws at 50% each leave 25% of the background showing through an opaque overlap (alpha 191 instead of 255), which reads as a flash. One mixed draw has no such dip.
- Derive outlines and shadows from the mixed result, and clear the mix state when another switch interrupts it.
- `review/transition-NN-MM.png` shows each dissolve step from the clip's last frame to the target's entry frame.

## Clocks: fixed step, interpolation and hit-stop

Keep four clocks apart: the simulation tick, the display refresh, the pose (which frame is shown) and any export or recording rate. A 60 Hz display does not turn eight drawings into sixty, and a low-rate recording can exaggerate uneven holds.

- Run the simulation at a fixed step, usually 60 Hz, with an accumulator. Draw at the display rate and interpolate positions between the last two simulation states.
- Pick poses from the simulation clock (ticks or milliseconds since the clip started), never from the display frame count. Durations authored in ticks then change on exact updates; `tick_grid` in the manifest shows how millisecond timing lands on the grid.
- **Hit-stop** freezes the simulation clock for `hitstop_ticks` when a `hit` or `impact` event fires. Movement, timers and the attacker's clip time all stop, on the strike pose. A separate presentation clock keeps running for screen shake, flashes and ambient loops. Do not stop the whole game loop.

## Time-warp onto the gameplay hit

When gameplay fixes the moment of impact (a parry window, a projectile arrival, a rhythm beat), warp clip time so the authored `hit` lands on that tick. Map clip time piecewise-linearly:

- `0` stays at `0`.
- The authored hit time `events_ms[hit].at_ms` maps to the gameplay hit time.
- The clip end maps to the gameplay end.

The wind-up stretches or compresses, and the strike pose still shows exactly on the hit. For several hits, warp each span between consecutive hits. Keep each span's factor within about 0.5 to 2; beyond that, author a different clip. Enemy wind-ups must stay at or above the 28-tick telegraph after warping.

## Pixel snapping and sampling

- Display pixel art at whole-number scales with nearest-neighbour sampling (`sampling: "nearest"` in the manifest). Painted art uses linear sampling.
- Snap the drawn root to whole display pixels: compute the world position, scale it, then round once. Rounding each body part separately shimmers.
- Do not resample frames at runtime per frame or per zoom step. Pick an integer zoom, or bake a scaled set once.
- Facing flips mirror about the anchor x. `review/turn-test.png` shows each clip's entry pose beside its mirror, and `turn_slide_px` says how far the stance jumps on a turn. Flip only art whose handed details (weapon hand, text, scars) survive mirroring.

## States and contact

For a platformer, pick meaningful idle, run, rise, apex, fall and land states. Velocity and ground contact choose airborne states; a timer must not land the character before collision does. Rise, apex and fall can start as separately authored stills; land can be a short one-shot. If those poses have not been generated, record the temporary reuse instead of claiming a full jump set.

A held landing pose while the collider keeps moving slides by `speed * hold_duration`. For a moving landing, blend a short recovery into the continuing gait; keep a held crouch for stopped landings. Never change jump physics to hide a missing pose.

## Diagnose stutter before adding frames

Record the actual render intervals and pose indices for a short representative run; a clean automated trace does not prove performance on the user's device. Compare the game, a gallery page and the exported preview at the same movement speed. A gallery must use the manifest's durations and an explicit playback multiplier rather than its own fixed frame rate. For distance-driven motion its cycle time is `stride / abs(speed)`.

Inspect state boundaries as well as loops. Rank large adjacent-frame changes, including last to first, to find review candidates, but never use pixel-difference scores, hash counts or frame counts as a quality score. Check head and costume drift separately from intended limb motion. If one transition is bad, repair the poses involved, or add controlled in-betweens, while keeping the accepted frames and registration. Generating ever larger atlases multiplies identity errors.

For constant-rate frame sequences, the video skill's [animation review helper](../../video2dsprite/scripts/animation_review.py) builds light and dark previews, frame-step views and interval selection; give it the real source rate. For variable durations, use the clip builder's previews.

## Engine and editor notes

Checked **2026-09-11**. The recommendations above are this skill's design guidance; these sources establish storage and runtime mechanics, not artistic quality.

- Aseprite separates sheet layout from animation frames and can export selected tags; a matrix is a storage layout. [Sprite sheets](https://www.aseprite.org/docs/sprite-sheet/)
- Tags group frame ranges with a playback direction; frame durations are edited separately. [Tags](https://www.aseprite.org/docs/tags/), [Frame Duration](https://www.aseprite.org/docs/frame-duration/)
- Slice pivots describe a base or central location; keep the coordinate conversion when using them as runtime origins. Lua `Frame.duration` is in seconds, while `.aseprite` frame headers store milliseconds. [Slices](https://www.aseprite.org/docs/slices/), [Frame API](https://www.aseprite.org/api/frame#frameduration), [File specification](https://github.com/aseprite/aseprite/blob/main/docs/ase-file-specs.md)
- Godot `SpriteFrames` stores a per-animation speed and per-frame relative durations: seconds = `relative_duration / (animation_fps * abs(playing_speed))`. For an imported duration of `ms` at speed 1, use `relative_duration = ms * animation_fps / 1000`. Check the target version's loop API. [SpriteFrames](https://docs.godotengine.org/en/stable/classes/class_spriteframes.html)
- Godot documents part-based cutout animation, pivots and draw order, and mixing it with frame animation. [Cutout animation](https://docs.godotengine.org/en/stable/tutorials/animation/cutout_animation.html)
