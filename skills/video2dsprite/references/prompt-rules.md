# Image-to-video motion briefs

Use approved art as the visual reference and generate each action separately. Keep the
input image, prompt, returned clip and request/job record. API generation is provided by
[generate2dmedia](../../generate2dmedia/SKILL.md); providers differ in duration,
resolution, reference types and first/last-frame controls, so never assume one schema.

Run commands from the user's project root. `<skill-dir>` is this skill's folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs stay in the project.

## Write the brief with prepare_i2v_input

`prepare_i2v_input.py prepare` places the master on the provider canvas at a recorded
scale and root, picks a key colour the master does not use, and writes the prompt from
the action's timeline template. The same numbers go into `registration_job.json`, so
`register_clip.py` can undo the placement exactly (registration by construction).

```bash
python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/hero.png --action walk --subject "compact pixel-art knight with a red scarf" --facing right --output-dir jobs/hero-walk
python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/hero.png --action attack --pixel-art --output-dir jobs/hero-attack
python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/heroes-sheet.png --view-box 557,0,1115,941 --action idle --output-dir jobs/hero-front-idle
python "<skill-dir>/scripts/prepare_i2v_input.py" prepare --master art/heal.png --action fx --output-dir jobs/heal-fx
python "<skill-dir>/scripts/prepare_i2v_input.py" lint --prompt-file my-prompt.txt --key magenta
```

Send `input.png` and `prompt.txt` to the provider. `review-guide.png` shows the work
region and root for your own check; never send it. Pixel art (`--pixel-art`) uses an
integer NEAREST scale at an integer offset. A custom `--prompt-file` may use
`{work_region}`, `{root}`, `{canvas}`, `{key}`, `{timeline}` and `{duration}`.

## The prompt contract

Every generated prompt has these lines, in this order:

1. **Identity:** "Animate this EXACT isolated ..." plus the `--subject` description,
   facing and style (pixel clusters or drawing style, no 3D redesign).
2. **Locked camera:** fixed framing and scale; no zoom, push-in, pull-out, pan, tilt,
   shake, rotation toward the camera or cut.
3. **Flat key:** a perfectly uniform key colour for the whole clip; no floor, shadow,
   horizon, gradient, vignette, scenery, text or key-coloured light.
4. **Numeric work region:** "stays inside the work region x=126..1153, y=20..687 of
   this 1280x720 image". It is the padded source canvas on the provider canvas, so
   everything inside it survives registration.
5. **Root:** "Keep the root fixed at pixel (640, 620): the feet stay on the same
   invisible ground line at y=620." (effects: centred on a pixel).
6. **Motion:** the action, with amplitude and cycle wording; walks and runs are
   treadmill motion in place.
7. **Timeline** in seconds, starting with the early calm span.
8. **Body only** (characters): the game adds effects; no particles, sparks, energy,
   rings, starbursts, pulsing glow, speed lines or dust.
9. **Avoid:** the action's negatives, plus extra limbs, new objects, text, camera motion.

Describe the character by what it looks like. Never use famous names (franchises,
characters, artists, studios): a name drags in that design's features (hair, props,
mounts) and fights the supplied art. "Clean-shaven" and "sleek hair" fix drift better
than any name.

## Timelines

Templates are written for a 6 s take and retimed for `--duration`; the first 0.4 s
always stays a calm span.

| Action | Timeline (6 s) | Ends at rest |
|---|---|---|
| idle | 0.0-0.4 hold the reference; 0.4-5.6 two slow breathing cycles; 5.6-6.0 settle into the start pose | yes |
| walk | 0.0-0.4 hold; 0.4-5.0 walk in place, a left-right pair about every 0.8 s; 5.0-5.6 finish the step, feet back to the stance; 5.6-6.0 hold | yes |
| run | 0.0-0.4 hold; 0.4-5.0 run in place, a pair of strides about every 0.5 s; 5.0-5.6 stop; 5.6-6.0 hold | yes |
| attack | 0.0-0.4 hold; 0.4-1.2 anticipation; 1.2-1.7 ONE strike; 1.7-3.2 recoil and return; 3.2-6.0 hold neutral | yes |
| cast | 0.0-0.4 hold; 0.4-1.2 raise into the casting pose; 1.2-2.0 sustain; 2.0-3.5 return; 3.5-6.0 hold neutral | yes |
| guard | 0.0-0.4 hold; 0.4-1.3 brace; 1.3-3.5 HOLD the guard; 3.5-4.5 relax; 4.5-6.0 hold neutral | yes |
| hurt | 0.0-0.4 hold; 0.4-0.8 wince and recoil; 0.8-1.8 recover; 1.8-6.0 hold neutral | yes |
| victory | 0.0-0.4 hold; 0.4-1.8 one celebration; 1.8-6.0 HOLD the victory pose | no |
| defeat | 0.0-0.4 hold; 0.4-2.4 slump into a compact kneel; 2.4-6.0 remain still | no |
| ambient | 0.0-0.4 hold; 0.4-5.6 a visible cycle about every 2 s; 5.6-6.0 back to the reference | yes |
| fx | 0.0-0.4 hold the effect; 0.4-4.0 grow, peak, dissipate until invisible; 4.0-6.0 empty key | no |

Prompt times are a direction, not a guarantee: judge the returned timing and pick the
impact frame and the loop interval yourself (gait_loop and retime). The long holds give
retime room to cut a clean one-shot.

## Amplitude and cycle

Ask for motion that survives game scale, with a cycle length.

| Motion | Say | Not |
|---|---|---|
| idle breathing | chest and shoulders rise and settle by a few pixels; one slow cycle about every 2.6 s | "tiny", "barely", "very subtle" |
| walk | one full left-right pair about every 0.8 s; heel contact, passing, toe-off clearly visible | "slow walk", "slow-motion" |
| run | one pair of strides about every 0.5 s; push-off and a brief flight phase | "running across the screen" |
| sway (trees, cloth) | about 2 to 4 percent of the moving part's size, a cycle about every 2 s | "extremely slow", "nearly static" |
| one-shot action | one readable anticipation, one strike, a full return | "repeated attacks" |

`prepare_i2v_input.py lint` warns on amplitude killers (`tiny`, `extremely slow`,
`very slow`, `very fine`, `very subtle`, `barely`, `imperceptible`, `nearly static`,
`almost still`, `slow motion`), camera moves, alpha requests, key-coloured light, and a
missing locked camera, work region or timeline. Negated wording ("no slow-motion freeze")
is not flagged. `--strict-lint` turns warnings into a failed run.

## Negatives that matter

| Negative | Why |
|---|---|
| no rings, circles or ripples | generators draw concentric rings on water and glows; they read as UI, not as the scene |
| no starbursts or cross flares | big star shapes cover faces and break the art style |
| no pulsing glow or global brightness changes | pulsing exposure flickers after keying and breaks loops |
| no zoom or push-in | a push-in changes the body scale; registration by construction assumes a locked camera (qc rejects it) |
| no key-coloured light | light in the key's hue (magenta, pink or purple glow on a magenta key) is keyed out with the background |
| no floor, contact shadow or gradient | anything that is not the flat key is kept as subject or leaves a dirty edge |

## Characters and creatures

Use a key colour absent from the costume: `prepare` picks magenta, then green, then blue,
and refuses a key the master's own colours lean to (`--allow-key-conflict` records the
override). A prompt that asks for transparency does not make the returned codec carry
alpha. Leave room for weapon, hair and cloth extremes: one-shot actions get action
padding (on a 448 px master: 96, 80, 96 and 24 px) and the work region covers it.

## Props and local background details

Trees: root and trunk stay stationary, only small branch and leaf response to breeze.
Buildings: fixed walls and roof; smoke, banners or lamps move independently. Water and
fire: constrain the animation to the selected region and keep structural edges. Never
ask a whole town or forest to sway: that brings camera and landmark drift. Use
`--action ambient`.

```text
Animate only this tree's outer leaves with a gentle, clearly visible breeze, about 2 to 4
percent of the crown width, one cycle about every 2 seconds. The roots and trunk base
stay fixed. Preserve the exact silhouette, scale, camera, colours and flat background.
No new branches, falling objects, rings or camera motion.
```

For scenic backgrounds, prefer `static master + local animated patch + feathered mask`
when architecture must stay stable, and keep the patch's source rectangle. Use
[generate2dmap](../../generate2dmap/SKILL.md) for navigation, collision, layer order,
parallax and coverage; video generation must not redefine walkable geometry.

## Effects

`--action fx` centres the effect, pads the canvas (160 px on 448 px) and asks for one
play that dissipates until invisible, then two seconds of empty key. `register_clip.py
apply --profile fx` fades the edges and dissolves the tail, so escaping energy never
ends at a hard edge. Keep screen-wide particles separate from character body motion.

## Loop review

If supported, the approved start image can also guide the last frame. That helps the
endpoints but does not guarantee a clean cycle, fixed contact or stable identity. Watch
the clip, choose a real interval and inspect normal-speed repeats on dark and light
backgrounds. Never hide a bad loop by stretching each frame's body, freezing its alpha
or crossfading a one-shot attack. Prefer a shorter good segment or a fresh generation.
