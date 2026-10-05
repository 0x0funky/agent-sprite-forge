# Image-to-video motion briefs

Use approved art as the visual reference. Record source canvas, anchor, palette,
silhouette and facing. Generate each action separately; keep the original input,
prompt, returned clip and request/job record. API generation is provided by
[generate2dmedia](../../generate2dmedia/SKILL.md); don't assume all providers expose
the same duration/resolution, reference types or first/last-frame controls.

## Characters and creatures

Prepare a full-body still with room for weapon/hair/cloth extremes. Use a flat key
color absent from the costume, or a provider-supported transparent output. A model
prompt requesting transparency does not establish that the returned codec has alpha.
The bundled keyer supports magenta; other colors need an appropriate external mask
or keyer, not a silently mismatched setting.

Idle:

```text
Animate this exact character in place, fixed three-quarter facing and locked camera.
Subtle breathing in shoulders and scarf; feet remain planted at their original points.
Preserve body scale, costume, face and pixel-art clusters. Keep the flat magenta
background unchanged, no ground shadow, no camera drift or added effects.
```

Walk:

```text
The same character walks in place facing right, a clear alternating step and arm
swing with natural contact and passing poses. The character stays at the same frame
position and scale; locked camera, unchanged flat magenta background, no scene change.
```

Attack:

```text
One readable sword attack: a brief anticipation, forward slash, then recovery to
the original ready pose. Fixed facing and camera, planted root, preserve the exact
character and weapon. Keep the blade and cloth inside the existing margins and
the flat magenta background unchanged. No additional enemies or full-screen flashes.
```

Keep screen-wide spell particles separate from character body motion. Judge the
returned timing and choose the impact frame explicitly; prompts don't enforce it.

## Props and local background details

Trees: root/trunk silhouette stationary, only small branch/leaf response to breeze.
Buildings: fixed wall/roof geometry; smoke, banners or lamps move independently.
Water/fire: constrain animation to the selected region and preserve structural edges.
Avoid asking an entire town/forest to sway: that introduces camera and landmark drift.

```text
Animate only this tree's outer leaves with a gentle breeze and occasional small
branch flex. The roots and trunk base stay fixed. Preserve exact silhouette scale,
camera, colors and empty background. No new branches, falling objects or camera motion.
```

For scenic backgrounds, prefer `static master + local animated patch + feathered mask`
when architecture must stay stable. Create a generous crop around the moving part
and retain its source rectangle, then composite it over the unchanged master.
The mask constrains placement; the motion source still needs review to avoid seams.
Use [generate2dmap](../../generate2dmap/SKILL.md) for navigation/collision, layer order,
parallax and scene coverage. Video generation must not redefine walkable geometry.

## Loop review

If supported, the approved start image can also guide the last frame. This helps
the endpoints but does not guarantee a clean cycle, fixed contact or stable identity.
Watch the clip, choose a real interval, and inspect normal-speed repeats on dark and
light backgrounds. Do not hide a bad loop by stretching each frame's body, freezing
its alpha, or blindly crossfading a one-shot attack. Prefer a shorter good segment
or a fresh bounded generation when the usable motion is insufficient.
