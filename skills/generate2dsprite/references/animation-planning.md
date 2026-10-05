# Animation planning: poses, phases, timing and telegraphs

Decide what the player must read before choosing a sheet size or generating anything. This reference covers the design side: identity, pose counts, leg phases, timing in ticks, action keys and enemy telegraphs. Packaging is in [frames-and-clips.md](frames-and-clips.md); playing the result in a game is in [runtime-integration.md](runtime-integration.md). The gait examples target a side-view biped. Keep the project's own camera and anatomy for top-down actors, quadrupeds and flying characters.

## Design a readable character first

Accept one master at the intended game scale. Choose a recognizable silhouette, head and body proportions, costume colour groups, and one or two identity details. Keep a small, consistent vocabulary of pixel clusters: a mark that disappears at gameplay size should not drive nine separately redrawn microtextures. Record the logical design grid, measured source dimensions, intended displayed body height, palette, light direction, view and facing.

A prompt such as "64x64 pixel character" describes an intended scale; it does not prove the returned pixel geometry. Inspect the actual output. For strict pixel art, keep the accepted logical grid and use nearest-neighbour, integer display scaling. When a reduction to a logical grid is needed, apply one shared reduction to the whole set and inspect it; never resize each pose to its own bounding box.

Use the same accepted master in every generation through the tool's real reference input. State identity and camera invariants separately from the allowed pose change. A pose or layout guide supplies geometry only. Keep slashes, dust, muzzle flashes and impact effects separate from the body.

## Choose poses and clips before sheet dimensions

For a new run or walk, **8 to 12 useful poses per action and direction** is a practical start unless deliberately sparse animation is wanted. Four-pose cycles stay valid as an intentional style. A low count is a prompt to inspect contact and passing coverage, weight transfer and the loop seam; a high count is not proof of good motion. Never pad an incomplete cycle with copies.

`3x3` is packing, not smoothness. Nine near-identical poses can look stiff; four well-spaced poses can suit a restrained style. Choose a sheet for economical coherent phases, or separate full-canvas frames when each pose needs more detail; separate calls must keep the same master and registration contract. A small locomotion prototype can hold **eight run phases plus one neutral idle** in nine cells. That is a narrow exception for one character's locomotion, not permission to put an unrelated attack, hurt and death set in one atlas.

### Name legs NEAR and FAR, not left and right

A side view cannot show which leg is anatomically left. In the 2026-10-05 fox trial, a run sheet prompted with left and right came back with the near (brighter) leg forward in all eight frames, so its two half-cycles looked alike. Name legs by depth instead: **NEAR** is the leg closer to the camera, **FAR** the one behind it. Arms swing opposite to the leading leg.

Row-major, zero-based plan for a side-view run:

| Index | Phase | Contact and motion |
|---|---|---|
| 0 | NEAR contact | The NEAR leg leads into contact; the FAR arm swings forward. |
| 1 | NEAR down | Weight settles over the NEAR support leg; knees compress. |
| 2 | NEAR passing | The FAR leg passes under the body; the NEAR foot prepares to push off. |
| 3 | Up toward FAR contact | The body rises and the limbs open toward the next lead; feet may clear the ground. |
| 4 | FAR contact | The FAR leg leads; the NEAR arm swings forward. Keep costume and weapon asymmetry. |
| 5 | FAR down | Weight over the FAR support leg, the same anatomical scale. |
| 6 | FAR passing | The NEAR leg passes under the body; do not repeat frame 2 unchanged. |
| 7 | Up toward NEAR contact | Continues into frame 0 without a second hold at the seam. |
| 8 | Neutral idle | A readable stance, excluded from the run loop. |

The second half of a cycle must lead with the other leg: frame `i` and frame `i + n/2` show opposite legs forward. If they show the same leg, regenerate frames 4 to 7 only. A walk keeps one foot on the ground at all times and has no airborne phase; a run does. These phase names are a brief, not proof that the model produced correct biomechanics.

### Compact prompt contract

```text
Use the supplied accepted character as the exact identity, palette, anatomy, camera and pixel-design reference. Create a 3x3 equal-cell sheet in row-major order: the eight specified run phases, then one neutral idle. The run plays cells 0-7 only; cell 8 is a separate idle.

Keep the same cell canvas, camera distance, anatomical scale, costume, handedness and facing. Use one fixed registration origin per cell. Animate the torso, pelvis and limbs around that origin: modest bob and airborne clearance are intended. Only contact phases put the support foot on the shared ground line. Do not pin every lowest pixel to that line or give every pose the same bounding box.

Name legs by depth: NEAR is the leg closer to the camera, FAR the one behind. Cells 0-3 lead with the NEAR leg, cells 4-7 with the FAR leg. <Insert the phase list.> Keep clear limb separation at gameplay scale; no extra fingers, detached shadows, dust or weapon effects. The whole motion envelope fits every cell with margin. Background: <verified transparency or a uniform chroma key>. No labels, numbers, grid lines or painted checkerboard.
```

### Leave room for the whole action

Reserve about 15% clear padding on every side of each cell around the **whole action envelope**: tail, ears, limbs and held equipment. If the envelope is too large, reduce one shared scale; never shrink only the widest pose. A tail or boot can still cross an invisible grid line in an orderly-looking sheet. `assemble_frames.py` checks for that before slicing and can keep the overflowing component whole (`--slice ownership`); see [frames-and-clips.md](frames-and-clips.md). [prompt-rules.md](prompt-rules.md) covers recovery.

## Timing in ticks

Games usually update at a fixed 60 Hz. Author durations as **ticks** of that clock so each pose shows for a whole number of updates:

| Ticks at 60 Hz | Duration | Rate |
|---|---|---|
| 4 | 66.7 ms | 15 fps |
| 5 | 83.3 ms | 12 fps |
| 6 | 100 ms | 10 fps |
| 8 | 133.3 ms | 7.5 fps |

Uniform millisecond timing such as 80 ms shows for 5, 5, 4 and 5 ticks at 60 Hz, and the clip stutters. The clip builder reports this, and `ticks` avoid it.

Give key poses longer durations instead of duplicate frames. A contact or impact pose can hold two or three times longer than a passing pose. Two consecutive frames that are almost identical become an unintended hold, and the builder flags them.

Choose the loop policy per action:

| Action | Policy | Notes |
|---|---|---|
| Idle, breathing, cloth or foliage sway | `cycle`, or `pingpong` for a simple back-and-forth | Pingpong never repeats the end frames. |
| Walk, run | `cycle` | A full gait with both leads; never pingpong a gait. |
| Attack, cast | `oneshot` | Wind-up, one strike, recovery. Never play the attack backwards to fake recovery. |
| Guard | `oneshot` into a holdable pose | Mark the held pose with a `hold` event. |
| Hurt | `oneshot` | One short reaction, not a loop. |
| Victory, defeat | `oneshot` | A start, a completion and an end pose that can stay on screen. |

Treat the provider's clip length as a suggestion. Pick the start, hit, recovery and hold frames from the frames you actually have.

## Keys and events

Mark the poses gameplay cares about. In a v2 clips manifest, `keys` names the anatomy of an action and `events` names the moments code reacts to:

- `keys`: `wind_start` (anticipation begins), `wind_peak` (furthest back), `strike` (the contact pose), `recover` (back toward neutral), `end`.
- `events`: `tell` (the readable warning), `hit` (damage applies), `active_end` (the hit window closes), `cancel` (another move may interrupt), `chain` (a combo may continue), `impact` (the hit-stop and effects anchor), `hold`, `end`, `sfx`, `step_l` and `step_r` (footfalls), or `custom:<name>`.

The builder converts every event to the millisecond and tick at which it is shown, including both passes of a pingpong clip.

**Hit-stop** freezes the attacker on the strike pose for a few ticks so contact reads. Shipped tuning used about 4 to 6 ticks for ordinary hits, 8 for breaking a guard and up to 18 for a boss finish. Declare it as `hitstop_ticks` next to a `hit` or `impact` event.

## Enemy telegraphs

An enemy's attack is fair only when the player can see it coming. Measured players pressed dodge 13 to 21 ticks after a readable cue. Give every enemy attack **at least 28 ticks (about 467 ms at 60 Hz) between its `tell` and its `hit`**. The clip builder warns when an enemy clip (`"role": "enemy"`) is shorter, or has a `hit` without a `tell`. Without events it uses `keys.wind_start` to `keys.strike`.

Make the tell a silhouette change: a raised weapon, a crouch, a flash on the body. A particle effect that a busy scene can hide is not a tell. Attacks with ground markers or slow projectiles already telegraph through their effect; a fast melee hit needs the pose.
