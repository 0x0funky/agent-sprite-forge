# Image-to-video handoff

Use for continuous secondary motion or smooth short actions after accepting the source artwork: breathing NPCs, monster attacks, hair/capes, swaying foliage, fountains and other scene accents. Keep strict pixel cel animation when it better fits the art/runtime. The generated source remains editable and recoverable.

## One representative asset first

Accept a master at gameplay size, then animate one short representative action through [video2dsprite](../../video2dsprite/SKILL.md). It may use a tool actually exposed by the host/daemon or the explicitly selected API route in [generate2dmedia](../../generate2dmedia/SKILL.md). Do not infer video tools from the agent brand. If no usable provider is available, deliver the prepared still/prompt and identify the missing capability.

Preserve source image and prompt, selected provider/model from its actual response, returned video, measured duration/frame rate/resolution and the trim/loop interval. Distinguish one-shot attack timing from a loop. Never present a returned MP4 as an alpha-ready game element.

## Handoff fields

- Stable asset/action IDs, accepted reference and reference role.
- Source canvas size and shared source-pixel anchor; display size/scale and facing.
- Allowed local motion versus fixed body/root/structure; full action envelope.
- Loop or one-shot; anticipation, contact and recovery timing; gameplay event clock remains authored code.
- Background/alpha strategy: verified alpha when available, or controlled matte followed by inspected keying/matting.
- Target devices, delivery alternatives and concurrent decoder budget.

Example creature idle prompt:

```text
Animate only this exact accepted creature, in place, on the same uniform matte.
Fixed orthographic camera, fixed source canvas and root. Preserve anatomy,
markings and standing-equivalent scale. One restrained breathing cycle: chest
expansion, small shoulder settle, delayed attached ornament motion, then return
to the reference stance. Feet remain planted; no body translation, new objects,
scene transition, zoom, dust cloud, ground shadow or background texture.
Keep the whole motion envelope inside the frame with the original padding.
```

For an attack, name anticipation → strike/contact → recoil → recovery explicitly. For a tree, keep trunk base/major branches fixed while leaf clusters move with small delayed motion. For water/flame, animate local material shapes without dragging the collision geometry. Ask for enough visible movement to survive gameplay scale; increasing whole-body sway is not the fix for a subtle loop.

## Preserve geometry through packaging

Use the **union** of motion bounds or original full canvas. Retain `sourceSize` and `sourceAnchor` (or documented equivalent) when cropping/scaling; for crop origin C, scale s and source root R, output root is `(R-C)*s`. Per-frame bbox fitting destroys registration and changes apparent body scale. Existing colliders and ground contact stay separate from alpha bounds.

Inspect first, middle, peak and last frames, then actual playback. Check identity drift, alpha fringes, limb duplication, planted feet, loop seam and full action bounds over both light and dark backgrounds. Do not ping-pong a run/attack to conceal a wrong return phase. Reject camera travel or structural warping rather than trying to align it away.

The video skill owns extraction and delivery. Browser cutouts may use verified transparent video, packed RGB/alpha decoded together, or frame/atlas fallbacks. Decoder support must be tested on target devices; a transparent desktop preview does not establish iPhone support. Several large clips can cost more decode/GPU work than a sheet even if download bytes are smaller. Pause hidden/irrelevant clips and test the actual concurrent scene.

Keep master art and fallback frame available. Expand to other assets/actions only after the sample passes the intended runtime review. Major bridges, walls and terrain normally remain static; animate detachable accents without moving structural/collision roots.
