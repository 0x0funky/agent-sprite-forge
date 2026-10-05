# Character animation

Use these references when a character needs convincing movement at gameplay size, explicit clips, or a state-driven animation set. They are split by when you need them:

| Step | Read | Covers |
|---|---|---|
| Before generating | [animation-planning.md](animation-planning.md) | A readable master, pose counts, NEAR/FAR leg phases, padding for the whole action, timing in ticks, loop policy per action, keys and events, hit-stop and enemy telegraphs (at least 28 ticks) |
| Packaging frames | [frames-and-clips.md](frames-and-clips.md) | `build_animation_clips.py` (clips v1 and v2, tick-grid report, events, transitions, reviews, lints) and `assemble_frames.py` (complete frames, keying, crop boxes, spill-safe slicing, loop tools) |
| In the game | [runtime-integration.md](runtime-integration.md) | The root and source coordinates, walk phase driven by distance travelled, entry frames, single-draw dissolves, fixed-step clocks and hit-stop, time-warp onto hits, pixel snapping, states, stutter diagnosis, engine notes |

Generation prompts are in [prompt-rules.md](prompt-rules.md). Processing generated sheets (keying, scale and anchors) is in [processing.md](processing.md). Fluid image-to-video motion is handed to the video skill as described in [video-handoff.md](video-handoff.md).

## The usual path

1. Accept one master at game scale. Then plan the action's phases with NEAR and FAR legs, the envelope padding and the timing in ticks.
2. Generate the action against the master. Image sheets go through `generate2dsprite.py process`. Code-art frames do not; they are already registered.
3. Package registered frames with `build_animation_clips.py`, or complete frames with `assemble_frames.py`. Read the lints and look at the review sheets: filmstrips on light, dark and checker backgrounds, onion skins, the turn test and the dissolves.
4. Wire the clips into the game: distance-driven gait, entry frames, transition hints, hit-stop and events on the simulation clock.

## Cutout or hybrid motion

When continuous motion and a stable identity matter more than a hand-drawn pixel grid, consider a part-based rig instead of more generated frames. The codeart2d skill's rig_animate tool (FK and two-bone IK with a ground constraint, easing, and vector or pixel output) renders registered frames plus a clips manifest. Those frames go straight to `build_animation_clips.py`.

Specify the camera, joint overlap, pivots, facing and draw order of the parts, and inspect the assembled rest pose before expanding clips. Check planted feet in world coordinates, fixed limb lengths, reachable joint targets, depth order and cycle seams. Rotated low-resolution parts can shimmer or look like a puppet even with nearest-neighbour sampling, so evaluate at gameplay size and keep the frame-by-frame route for comparison.

A hybrid keeps a stable head and body while swapping a few authored hands, feet, expressions or action poses. Use it only where it improves the requested style. Never replace an accepted pixel aesthetic with a rig just because it produces many samples cheaply, and never present optical-flow or blended in-betweens as newly drawn pixel animation.

## Review what the player will see

Play each run in place and while moving over a visible ground line, at the real display scale and in both facings. Check the following:

- Contact, down, passing and up differences, and that the two half-cycles lead with opposite legs.
- Planted-foot slide, deliberate bob and silhouette separation.
- Costume consistency, transition pops and the last-to-first seam.
- Alpha edges over light and dark backgrounds, clip indices and durations.

Step through the frames to tell a bad pose from a bad anchor or timing before regenerating. Keep raw masters and sheets and the source-to-export mapping, so processing damage can be fixed without rerolling good art. Numeric checks find symptoms; they do not establish appealing anatomy, convincing motion or visual polish.
