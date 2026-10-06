---
name: video2dsprite
description: Turn one approved master still into a character's whole animated sprite set - one image-to-video clip per action (idle, walk, run, attack, jump, hurt, cast and so on), all from that still, generated through generate2dmedia route_media.py (the xAI API when a key is configured, else the local Grok CLI) - then gate each take, key it with a soft matte, register it to the master, pick loops or retime actions, finish (HD by default, pixel on request) and package fixed-canvas frames, alpha WebM, packed MP4 for iPhone and a PNG fallback with engine metadata and a JS runtime. Also processes a supplied clip. Use for character and creature animation, fluid motion and any video-to-sprite work. Not for FX, icon or prop sheets (generate2dsprite), maps or plate loops (generate2dmap), code-drawn animation (codeart2d, only on request) or calling a video API by itself (generate2dmedia).
---

# Video to 2D Animation

An approved still sets identity and geometry, a video model supplies motion, and deterministic
tools prepare it for the engine. Video is not promised to be smaller, seamless or cheaper than sheets.

## Use when

- Animating a character or creature: every action comes from one approved master still, one clip per action.
- Fluid motion from an accepted master: idle breathing, walks, runs, attacks, casts, hair, capes, creatures.
- A supplied clip must become transparent, registered, looped frames and engine video.

## Do not use when

- FX, icon or prop sheets: [generate2dsprite](../generate2dsprite/SKILL.md).
- Water, fire or mist inside a still scene plate: [generate2dmap](../generate2dmap/SKILL.md). Only an API or CLI call: [generate2dmedia](../generate2dmedia/SKILL.md).
- Code-drawn animation the user asked for: [codeart2d](../codeart2d/SKILL.md).

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Its `ROUTES` block shows which API keys are configured (yes or no), the local readiness and the resolved order. If `encoding.stdout` fails, set `PYTHONUTF8=1`. `python "<skill-dir>/scripts/video2dsprite.py" doctor` checks ffmpeg (5.1+, VP9 alpha round trip).

## Art source

The master still comes from [generate2dsprite](../generate2dsprite/SKILL.md) (image generation, approved by the user) or from the user. Motion is image-to-video, one clip per action from that master; record the route as `art_source` and name it in your reply:

1. **API**, when a key is configured: xAI Grok Imagine Video (`XAI_API_KEY`), BytePlus Seedance (`ARK_API_KEY`) or fal.ai (`FAL_KEY`: Kling v3, Veo 3.1, Luma Ray 3.2, MiniMax H3, Wan 3.0, Vidu Q3, LTX) ([route-media.md](../generate2dmedia/references/route-media.md)). The configured key is the owner's consent, so there is no per-call question. `--last-frame <still>` pins idle and attack clips to end on their first pose.
2. **Local**: your own image-to-video tool (Grok's native `image_to_video`) when you have one, otherwise the user's Grok CLI in ACP mode (it cannot pin a last frame; the result says `lastFrameUsed`).
3. A clip the user supplies (no account needed).
4. codeart2d only when the user explicitly asks for code-drawn animation, or when `route_media.py` prints `no-route` (exit 3); otherwise prepare the job and prompt, explain the gap, and never invent a successful generation.

Every clip goes through one command: `python "<skill-dir>/../generate2dmedia/scripts/route_media.py" video --prompt-file <job>/prompt.txt --reference <job>/input.png --last-frame <job>/input.png --duration 6 --resolution 720p --out-dir <new>`. It prints one JSON line with `route`, `artifact`, `sha256`, `estimateUsd` and `lastFrameUsed`. The finish is HD by default; the pixel finish is an option when the user asks for pixel art.

## A whole sprite set from one still

For a character's game set, use `sprite_set.py`: one approved master (generate2dsprite `master.json`) in, one image-to-video clip per action out, keyed, gated, registered, cut, finished and packaged. For one clip or a supplied video, use the pipeline below. Details: [sprite-set.md](references/sprite-set.md). Prompts and fix clauses: [motion-prompts.md](references/motion-prompts.md).

1. **Plan.** `python "<skill-dir>/scripts/sprite_set.py" plan --master <art>/master.json --output-dir <set>`. Defaults: `--actions idle,walk,run,attack,jump,hurt`, `--finish` from the master (hd default), `--target-height` 256 hd or 80 pixel. Jump gets a 3:4 canvas with 34% headroom and attack a 16:9 one; idle and attack pin the end frame to the master. Read each action's `prompt` in `<set>/set_plan.json`, and edit `motion`, `negatives` or `gates` there before the run if needed.
2. **Run.** `python "<skill-dir>/scripts/sprite_set.py" run --plan <set>/set_plan.json`. Clips come from generate2dmedia `route_media.py`, then:
   - soft-matte keying;
   - numeric gates: area 0.72-1.32, feet, identity (a warning unless the design is lost), zoom, turning around, edge, background, extra objects, motion, end pose, colour bleed, one-shot timing;
   - up to 3 takes, each retake carrying the fix clause for the gate that failed; if every take fails, the best usable window is kept;
   - registration, then a loop, or a one-shot retimed to game length from its motion energy (attack 0.7 s with the hit at 40%, jump 0.9 s, hurt 0.5 s, cast 0.9 s; set in the plan's `timing`);
   - `finish_frames.py` hd or pixel, with the colour lock to the master on by default (`plan --no-colour-lock`);
   - png, webm and packed packages, verified.
   Run it again to resume; nothing is generated twice.
3. **No route (exit 3).** Make the clip with your own image-to-video tool from the input and prompt that the summary names, save it as the named `media/clip.mp4`, and run again.
4. **Review.** `python "<skill-dir>/scripts/sprite_set.py" review --plan <set>/set_plan.json`, then LOOK at every `-sheet.png`, every `-final.png` and the `lineup` (Read in Claude Code, view_image in Codex). The numbers cannot see identity, facing, a changed weapon, text or a covered face.
5. **Decide each action.**
   - Approve: `accept --plan <set>/set_plan.json --action <id> --take <n>`. Add `--window START:END` to keep part of a failed take.
   - Reject: `retake --plan <set>/set_plan.json --action <id> --fix <clause id or text>`, then `run` again. Clause ids include never-turn, weapon-shape, face-visible, no-text, no-flares and keep-colours.
6. **Report.** `python "<skill-dir>/scripts/sprite_set.py" report --plan <set>/set_plan.json` lists the routes, takes, QC numbers, loops, timing and package paths. In your reply, name the route that made each clip. Never call an action approved before you have looked at its sheet.

## Host notes

- **Codex:** no native image-to-video tool; `route_media.py` takes the xAI API or the Grok CLI. Look at review images with `view_image`.
- **Claude Code:** no media tools; `route_media.py` takes the route. Look at review PNGs and contact sheets with Read; run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
- **Grok:** its native `image_to_video` tool is your own tool; the same `grok` executable is the "Grok (local CLI)" route for other hosts.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it. Keep inputs and outputs inside the project. Every verb writes a new `--output-dir`: it refuses an existing one and publishes only after its checks (`--strict` fails on residue). Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Needs Python 3.10+, numpy, Pillow and ffmpeg 5.1+. `--help` lists every flag.

## Pipeline (one clip)

1. **Key plan.** `video2dsprite.py key-plan --master <master.png>`: paste `background_sentence` into the prompt; protect or recolour `design_colours_at_risk`.
2. **Prepare.** `prepare_i2v_input.py prepare --master <art> --action <idle|walk|run|attack|cast|guard|hurt|victory|defeat|ambient|fx> --output-dir <job>`; send `input.png` and `prompt.txt`; `registration_job.json` keeps the transform. An opaque master on a flat magenta, green or blue backdrop is keyed first (`--master-key` names another backdrop; the job records `masterKeying`). `--subject` is the whole identity phrase; the background line names its head ("adventurer with a scarf" gives "behind the adventurer"). Check a hand-written prompt with `prepare_i2v_input.py lint`.
3. **Generate** with `route_media.py video` (Art source above).
4. **Accept the take.** `register_clip.py qc --job <job>/registration_job.json --video <take>`; then `video2dsprite.py triage --video <take> --output-dir <new>`. A border touch inside the action means regenerate, never pad.
5. **Key.** `video2dsprite.py process --video <take> --output-dir <new> --reference <master.png>` (soft matte); read `frames-clean/matte-report.json` and look at frames over light and dark. Same keying for every clip of a character: `--matte-profile <character-profile.json>`. A design colour near the key: `--protect-color #rrggbb`. Already decoded frames (`frame_*.png` or `raw_*.png`, one frame keys a still): `video2dsprite.py clean --raw-dir <dir> --output-dir <new>`.
6. **Register.** `register_clip.py apply --job <job>/registration_job.json --frames <out>/frames-clean --output-dir <new>`; feet drift or jumps: `--lock feet`; spell or hit FX: `--profile fx`; one scale per character: `register_clip.py profile`, then `--character-profile`.
7. **Loop or retime.** Walks, runs, idles: `gait_loop.py select --frames-dir <reg>/frames --fps N --output-dir <new>` (`--kind idle` or `hover`); play `aids/loop3x.gif`. Attacks, casts, FX: `retime.py --frames-dir <reg>/frames --fps N --output-dir <new> --spans <spans>` (impact, hold, ticks), or for a slow-motion one-shot `--range 0:N --auto-oneshot --duration 700 --key hit=<impact>@0.4+80` (cuts frozen holds, pins the hit, compresses to game length); walk cadence: `gait_loop.py measure-stride`.
8. **Finish and package.** Finish the registered frames (Finish, below), then `engine_export.py package --clean-dir <fin>/frames --output-dir <new> --selection <selection.json> --source-size W,H --source-anchor X,Y --formats png,webm,packed --tiers actor` (W,H and X,Y from `finish.json`). Refused for key residue: re-key; `--allow-key-residue` only with a recorded override.
9. **Verify.** `engine_export.py verify --package <dir>`, then `validate_animation.py <character dir> --require-states idle,walk --require-verify`. `--require-states` matches each package's `--name` (a package named `hero-run` needs `--require-states hero-run`, not `run`).

Details: [pipeline.md](references/pipeline.md), keying [matte.md](references/matte.md), review and cuts [animation-review.md](references/animation-review.md), prompts [prompt-rules.md](references/prompt-rules.md).

## Finish (HD default, pixel option)

Finish every registered action before packaging. Finished frames keep the source names, so gait_loop and retime selections still apply. Details: [finishing.md](references/finishing.md).

- **HD (default):** `python "<skill-dir>/scripts/finish_frames.py" hd --frames <reg> --output-dir <new> --target-height <px> --role hero --character <id>`. The rest pose (frame 0 or `--rest-frame`) sets one scale. Box downscale with premultiplied alpha on a grid pinned to the feet, then clean alpha. Never nearest, never an upscale. `--display-sizes 1x,2x` rebuilds each size from the source.
- **Same character, other actions:** `--scale-ref <idle finish dir>` instead of `--target-height` (scale x sqrt(rest-area ratio), clamped to +-3%). A warning means the rest frame is not the base still.
- **Pixel (on request):** first `finish_frames.py palette build --frames <every action's reg dir> --colors 32 [--reserve #hex] --out <palette.json>`, then per action `finish_frames.py pixel ... --target-height <1/8 to 1/12 of the source> --palette <palette.json>`. The default is bold pixel art: a crisp 4x4-cluster downscale (no blurred in-between colours; lines and eyes survive), a light contrast and saturation lift, at most `--colors` (32) colours per frame, lone-pixel cleanup that keeps eyes, a 1 px selective outline (`--outline dark|none`), temporal hysteresis, binary alpha, no dither. `--canvas 48x64` gives fixed cells. Use `--loop-policy oneshot` for attacks and `--indexed-sheet` for an indexed sheet.
- **Colour lock:** `--colour-lock <keyed master.png>` keeps every design colour on the master's in OKLab (chroma only, lightness kept: no posterising) and smooths still pixels over time; `colour_lock.py measure` reports hue flips and region spread.
- **Cast check:** `finish_frames.py lineup --sets <finished dirs> --out <lineup.png>`. Roles come from `--role` or `ROLE=DIR`; the default rules are hero:1.0, mob:<=1.0, boss:~2.0, spirit:~1.0 (by area). Look at the x1/x3 sheet; a failed rule exits 1.
- **Review:** look at `review-contact.png` and read `qa` in the JSON line (`flipsPerPair`, `specksRemoved`, `alphaBinary`). The status stays `needs-visual-review`.
- **Package:** `engine_export.py package --clean-dir <fin>/frames --source-size W,H --source-anchor X,Y`, taking W,H and X,Y from `size` and `anchor` in `finish.json`. Do not pass `--registration`; pixel finishes add `--pixel-art --sampling nearest`.

## Motion and geometry rules

- One clip, one action. The game owns translation, hit timing, damage and state changes.
- One fixed transform per clip; never crop or resize frames to their own alpha bounds (it pumps size and pins airborne feet). Never freeze the still's alpha over moving RGB.
- All actions of one character share one body height, taken from the master's rest pose; never upscale a finished sprite.
- Choose cycles with `gait_loop.py select`, never by eye alone, then confirm by eye. Walks never ping-pong; attacks never recover by playing frames backwards; retime actions to their impact and hold. Walks ship cadence and stride.
- Keying claims quote the matte report's numbers and stay `needs-visual-review`. `--matte binary --despill-mode off` reproduces the cfed170 keyer.

## Runtime and acceptance

- Acceptance order: package, then verify, then `validate_animation.py`. Deliver `animation.json` (3.0, keeps every 2.0 key; whole-ms `durationsMs`, exact `fpsRational`), QA, poster, PNG fallback and the requested transports. Geometry stays in source units (`sourceSize`, `sourceAnchor`, `sourceRect`).
- Packed H.264 stores RGB left and alpha right; it is not a transparent MP4. Draw the compositor's `lease.drawable` ([packed-alpha-runtime.js](references/packed-alpha-runtime.js) over `references/runtime/packed-alpha-webgl.mjs`); show the poster until a decoded frame exists. Distance-driven walks, hit-stop and transitions: `references/runtime/forge-runtime.mjs`.
- Inspect at game scale: face and silhouette stability, contact, moving alpha edges over light and dark, loop seam, readability. Verify real iPhone playback before claiming alpha or frame rate; compare bytes and decode cost before replacing sheets.

Data contracts: `references/schemas/video.schema.json`.
