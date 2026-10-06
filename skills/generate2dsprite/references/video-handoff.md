# Image-to-video handoff

Use for continuous secondary motion or smooth short actions after accepting the source
artwork: breathing NPCs, monster attacks, hair and capes, swaying foliage, fountains and
other scene accents. Keep strict pixel cel animation when it fits the art and runtime
better. The generated source stays editable and recoverable.

## One representative asset first

Accept a master at gameplay size (the RGBA cut-out from `process`), then animate one
short representative action through [video2dsprite](../../video2dsprite/SKILL.md). Use
a tool the host or daemon actually exposes, or the explicitly selected API route in
[generate2dmedia](../../generate2dmedia/SKILL.md). Do not infer video tools from the
agent brand. If no provider is available, deliver the prepared input and prompt and name
the missing capability. Never present a returned MP4 as an alpha-ready game element.

## Registration by construction

Registration is decided before generation, not recovered from bounding boxes afterwards.
`prepare_i2v_input.py` places the master on the provider canvas at one recorded scale,
with its root on a fixed pixel (640, 620 on 1280x720), and writes that transform into
`registration_job.json`. `register_clip.py apply` applies its inverse to every frame of
the returned clip: one scale, one translation, so crouches, lunges and contact survive
and the body never pumps in size. A provider that returns another size is absorbed: the
same aspect is one factor (960x960 from a square input), a 1264x720 reply to 1280x720 is
a fixed centre crop (`--fit auto`), and `--fit stretch` covers a provider that resizes.

Run from the project root; `<video2dsprite-dir>` is the video2dsprite skill folder
(`${CLAUDE_SKILL_DIR}/../video2dsprite` in Claude Code).

```bash
python "<video2dsprite-dir>/scripts/prepare_i2v_input.py" prepare --master art/hero.png --action attack --subject "compact knight with a red scarf" --facing right --output-dir jobs/hero-attack
python "<video2dsprite-dir>/scripts/register_clip.py" qc --job jobs/hero-attack/registration_job.json --video takes/hero-attack-1.mp4
python "<video2dsprite-dir>/scripts/register_clip.py" apply --job jobs/hero-attack/registration_job.json --frames work/hero-attack-1/frames-clean --output-dir work/hero-attack-registered
python "<video2dsprite-dir>/scripts/register_clip.py" profile --registration work/hero-idle-registered/registration.json --id hero --output profiles/hero.json
python "<video2dsprite-dir>/scripts/register_clip.py" apply --job jobs/hero-walk/registration_job.json --frames work/hero-walk-1/frames-clean --lock feet --character-profile profiles/hero.json --output-dir work/hero-walk-registered
python "<video2dsprite-dir>/scripts/register_clip.py" validate-profile --profile profiles/hero.json --registration work/hero-idle-registered/registration.json --registration work/hero-walk-registered/registration.json
```

The full order: plan the key (video2dsprite key-plan), prepare the input, generate,
judge the take (`qc`), triage and key it (video2dsprite process), register (`apply`),
choose the loop or one-shot (gait_loop, retime), then package and verify.

- `qc` appends one line per take to `takes.jsonl` (kept or rejected, with reasons): edge
  key purity, landmark and identity NCC against the master, camera scale and drift in
  frame 0 and in the end calm span. A 12% push-in is rejected.
- `apply` writes `frames/`, `registration.json` (mode `construction`) and
  `review-contact.png`. It fails, writing nothing, when the generator cut the subject at
  the frame edge, when the subject leaves the padded canvas, or when the rest pose does
  not land on the master (anchor or height). Resampling leaves an invisible alpha 1-4
  halo with invented key-leaning colours; `apply` clears alpha 4 and below and records it
  (`hygiene` in registration.json), so the packaging residue gate measures real spill.
- `--lock feet|x|hip` pins the foot line and x to the rest pose in whole output pixels,
  for idles whose feet drift and for actions the game moves (jumps, knockback).
- A character profile pins one master, scale, anchor and matte setting for every clip of
  a character; `apply --character-profile` refuses a clip prepared at another scale. Its
  matte block pins `unmix` only for the soft matte (dominance and binary profiles pin it
  off) and `erode` in whole pixels, as video2dsprite `--matte-profile` reads them.

## Padding contract

The registered canvas is the master canvas plus the action padding `[left, top, right,
bottom]`, and the anchor moves by `(left, top)`. A 448x448 master anchored at [224, 430]
with attack padding [96, 80, 96, 24] registers onto 640x552 with the anchor at [320,
510]; the body keeps its size. The prompt's work region is that padded canvas on the
provider canvas, so everything the generator draws inside it survives.

- Content past the padded canvas but still in the video: grow the canvas with the
  `--action-padding` value `apply` prints. Never shrink the body to fit.
- Content cut by the video frame (a limb or blade touching the edge): **regenerate, don't
  pad**. Transparent padding cannot restore what the generator never drew.

## Early calm span

Every timeline starts with 0.0-0.4 s holding exactly the supplied reference pose. Frame 0
is the rest pose: `apply` checks it against the master, locks pin to it, and the profile
records its foot line. Returning actions also end in a calm span, which `qc` uses to
catch push-ins. Keep the calm span through registration and cut it later with retime;
never start a take mid-motion when you can regenerate.

## Handoff fields

- Stable asset and action ids, the accepted master and its role.
- Source canvas size and shared source-pixel anchor, display size and facing.
- Allowed local motion versus a fixed body, root or structure; the action envelope
  (padding) and the key colour from `prepare`.
- Loop or one-shot; anticipation, contact and recovery timing. The gameplay event clock
  stays in code.
- Background and alpha strategy: verified alpha when available, otherwise a controlled
  key followed by inspected keying.
- Target devices, delivery alternatives and the concurrent decoder budget.

For an attack, name anticipation, strike or contact, recoil and recovery. For a tree, keep
the trunk base and major branches fixed while leaf clusters move with a small delay. For
water or flame, animate local material shapes without dragging collision geometry. Ask
for motion that survives gameplay scale; whole-body sway is not the fix for a subtle loop.

## Preserve geometry through packaging

Keep `sourceSize` and `sourceAnchor` from `registration.json` (the padded canvas) when
cropping or scaling: for crop origin C, scale s and source root R, the output root is
`(R - C) * s`. Per-frame bounding-box fitting destroys registration and changes the
apparent body scale. Existing colliders and ground contact stay separate from alpha
bounds.

Inspect the first, middle, peak and last frames, then real playback: identity drift,
alpha fringes, duplicated limbs, planted feet, the loop seam and the full action bounds
over light and dark backgrounds. Do not ping-pong a run or attack to hide a wrong return
phase. Reject camera travel or structural warping rather than trying to align it away.

The video skill owns extraction and delivery. Browser cut-outs may use verified
transparent video, packed RGB and alpha decoded together, or frame and atlas fallbacks.
Test decoders on target devices: a transparent desktop preview does not prove iPhone
support, and several large clips can cost more decode and GPU work than a sheet. Pause
hidden clips and test the real concurrent scene.

Keep the master and a fallback frame. Expand to other assets and actions only after the
sample passes the intended runtime review. Bridges, walls and terrain normally stay
static; animate detachable accents without moving structural or collision roots.
