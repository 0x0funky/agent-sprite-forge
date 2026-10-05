---
name: video2dsprite
description: Turn an approved 2D character, creature, prop or scene detail into game animation through image-to-video, or process a supplied clip. Plan the chroma key and the image-to-video input, generate with the verified route, triage and key the clip with a soft matte, register it to the master, pick loops or retime actions, then package fixed-canvas frames, alpha WebM, packed MP4 for iPhone and a PNG fallback with engine metadata and a JS runtime. Use for fluid organic motion (breathing, hair, cloth, creature attacks) and any video-to-sprite work. Not for drawn pose sheets or small pixel animation (generate2dsprite, codeart2d), maps or plate loops (generate2dmap), or calling a video API by itself (generate2dmedia).
---

# Video to 2D Animation

An approved still sets identity and geometry, a video model supplies motion, and deterministic
tools prepare it for the engine. Video is not promised to be smaller, seamless or cheaper than sheets.

## Use when

- Fluid motion from an accepted master: idle breathing, walks, runs, attacks, casts, hair, capes, creatures.
- A supplied clip must become transparent, registered, looped frames and engine video.

## Do not use when

- Discrete pose sheets, exact pixel animation or game FX: [generate2dsprite](../generate2dsprite/SKILL.md) and [codeart2d](../codeart2d/SKILL.md).
- Water, fire or mist inside a still scene plate: [generate2dmap](../generate2dmap/SKILL.md). Only an API or CLI call: [generate2dmedia](../generate2dmedia/SKILL.md).

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Use only the routes its `ROUTES` block names, in its order. `ready` means usable now: your own tool, or Grok (local CLI) VERIFIED for the installed version. `consent` (the paid API) needs the user's consent for each request. An installed CLI is not a connected tool until a verified run ([cli-routes.md](../generate2dmedia/references/cli-routes.md)). If `encoding.stdout` fails, set `PYTHONUTF8=1`. `python "<skill-dir>/scripts/video2dsprite.py" doctor` checks ffmpeg (5.1+, VP9 alpha round trip).

## Art source

The master comes from [generate2dsprite](../generate2dsprite/SKILL.md) or the user, by its art-source rules (code art first for small pixel sprites, then the host image tool, a VERIFIED local agent, the paid API with consent, existing art). Small pixel loops and FX stay code art. Motion, local agent first; record the route as `art_source` and name it in your reply:

1. the host's own image-to-video tool, with its real schema;
2. Grok (local CLI) in ACP mode when the doctor reports it VERIFIED: `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" video --route auto --reference <job>/input.png --prompt-file <job>/prompt.txt --output-dir <new> --execute`. No per-call question within the session cap; say "Grok (local CLI)" ran;
3. the paid xAI REST API (generate2dmedia `generate_media.py video`) only with the user's explicit consent for that request;
4. a clip the user supplies (no account needed);
5. otherwise prepare the job and prompt, explain the gap, and never invent a successful generation.

## Host notes

- **Codex:** no native image-to-video tool; use route 2 or 3. Look at review images with `view_image`.
- **Claude Code:** no media tools. Look at review PNGs and contact sheets with Read; run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
- **Grok:** its native `image_to_video` tool is the host tool; the same `grok` executable is the "Grok (local CLI)" route for other hosts.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it. Keep inputs and outputs inside the project. Every verb writes a new `--output-dir`: it refuses an existing one and publishes only after its checks (`--strict` fails on residue). Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Needs Python 3.10+, numpy, Pillow and ffmpeg 5.1+. `--help` lists every flag.

## Pipeline

1. **Key plan.** `video2dsprite.py key-plan --master <master.png>`: paste `background_sentence` into the prompt; protect or recolour `design_colours_at_risk`.
2. **Prepare.** `prepare_i2v_input.py prepare --master <art> --action <idle|walk|run|attack|cast|guard|hurt|victory|defeat|ambient|fx> --output-dir <job>`; send `input.png` and `prompt.txt`; `registration_job.json` keeps the transform. Check a hand-written prompt with `prepare_i2v_input.py lint`.
3. **Generate** by the art-source order above.
4. **Accept the take.** `register_clip.py qc --job <job>/registration_job.json --video <take>`; then `video2dsprite.py triage --video <take> --output-dir <new>`. A border touch inside the action means regenerate, never pad.
5. **Key.** `video2dsprite.py process --video <take> --output-dir <new> --reference <master.png>` (soft matte); read `frames-clean/matte-report.json` and look at frames over light and dark. Same keying for every clip of a character: `--matte-profile <character-profile.json>`. A design colour near the key: `--protect-color #rrggbb`. Already decoded frames: `video2dsprite.py clean`.
6. **Register.** `register_clip.py apply --job <job>/registration_job.json --frames <out>/frames-clean --output-dir <new>`; feet drift or jumps: `--lock feet`; spell or hit FX: `--profile fx`; one scale per character: `register_clip.py profile`, then `--character-profile`.
7. **Loop or retime.** Walks, runs, idles: `gait_loop.py select --frames-dir <reg>/frames --fps N --output-dir <new>` (`--kind idle` or `hover`); play `aids/loop3x.gif`. Attacks, casts, FX: `retime.py --frames-dir <reg>/frames --fps N --output-dir <new> --spans <spans>` (impact, hold, ticks); walk cadence: `gait_loop.py measure-stride`.
8. **Package.** `engine_export.py package --clean-dir <reg>/frames --output-dir <new> --selection <selection.json> --registration <registration.json> --formats png,webm,packed --tiers actor`. Refused for key residue: re-key; `--allow-key-residue` only with a recorded override.
9. **Verify.** `engine_export.py verify --package <dir>`, then `validate_animation.py <character dir> --require-states idle,walk --require-verify`.

Details: [pipeline.md](references/pipeline.md), keying [matte.md](references/matte.md), review and cuts [animation-review.md](references/animation-review.md), prompts [prompt-rules.md](references/prompt-rules.md).

## Motion and geometry rules

- One clip, one action. The game owns translation, hit timing, damage and state changes.
- One fixed transform per clip; never crop or resize frames to their own alpha bounds (it pumps size and pins airborne feet). Never freeze the still's alpha over moving RGB.
- Choose cycles with `gait_loop.py select`, never by eye alone, then confirm by eye. Walks never ping-pong; attacks never recover by playing frames backwards; retime actions to their impact and hold. Walks ship cadence and stride.
- Keying claims quote the matte report's numbers and stay `needs-visual-review`. `--matte binary --despill-mode off` reproduces the cfed170 keyer.

## Runtime and acceptance

- Acceptance order: package, then verify, then `validate_animation.py`. Deliver `animation.json` (3.0, keeps every 2.0 key; whole-ms `durationsMs`, exact `fpsRational`), QA, poster, PNG fallback and the requested transports. Geometry stays in source units (`sourceSize`, `sourceAnchor`, `sourceRect`).
- Packed H.264 stores RGB left and alpha right; it is not a transparent MP4. Draw the compositor's `lease.drawable` ([packed-alpha-runtime.js](references/packed-alpha-runtime.js) over `references/runtime/packed-alpha-webgl.mjs`); show the poster until a decoded frame exists. Distance-driven walks, hit-stop and transitions: `references/runtime/forge-runtime.mjs`.
- Inspect at game scale: face and silhouette stability, contact, moving alpha edges over light and dark, loop seam, readability. Verify real iPhone playback before claiming alpha or frame rate; compare bytes and decode cost before replacing sheets.

Data contracts: `references/schemas/video.schema.json`.
