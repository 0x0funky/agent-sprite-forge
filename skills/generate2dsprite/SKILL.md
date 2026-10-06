---
name: generate2dsprite
description: Make game-ready 2D characters, creatures, props, icons and FX as master stills, sprite sheets or animation clips. Plan size, motion and finish; generate the art (image generation is the default, through generate2dmedia route_media.py - the API when a key is configured, else the local Codex or Grok CLI); approve one master still per character; then key, slice, register, scale, palette, QA and export frames for a game engine (Aseprite JSON, Godot SpriteFrames or Sprite3D). HD finish by default, pixel finish on request. Character animation is one image-to-video clip per action from the master (video2dsprite); sheets are for FX, icons and props. Use for sprites, masters, sheets, frame-by-frame animation and packaging frames from any source. Not for maps, tiles or backgrounds (generate2dmap), video clips (video2dsprite), code-drawn art (codeart2d, only on request) or calling an image or video API by itself (generate2dmedia).
---

# Generate 2D Sprite

Make the smallest asset bundle the game needs, at its real display size, camera and style.
Game logic (collision, damage, timing) stays independent of the art.

## Use when

- A character, creature, prop, item, icon or FX is needed as a master still, a sheet or clips.
- Frames already exist (generated, painted, code-drawn) and need clips, palettes or an engine export.

## Do not use when

- Maps, tiles, terrain, parallax or scene plates: [generate2dmap](../generate2dmap/SKILL.md).
- Animating a character: approve one master here, then [video2dsprite](../video2dsprite/SKILL.md) makes one clip per action ([video-handoff.md](references/video-handoff.md)).
- Code-drawn art the user asked for: [codeart2d](../codeart2d/SKILL.md). Only an API or CLI call: [generate2dmedia](../generate2dmedia/SKILL.md).

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Its `ROUTES` block shows which API keys are configured (yes or no), the local readiness and the resolved order. If `encoding.stdout` fails, set `PYTHONUTF8=1`.

## Art source

Image generation is the default art source for characters, creatures, props, icons and FX. Record it as `art_source` (`api`, `host_image`, `existing`, or `code` only on request) and name the route in your reply. A route the user names explicitly wins.

1. **API**, when a key is configured (`OPENAI_API_KEY` or `XAI_API_KEY`, in the environment or the user config file): the configured key is the owner's consent, so there is no per-call question.
2. **Local**: your own image tool (Codex `image_gen`, Grok's native tool) when you have one, otherwise the user's signed-in Codex CLI, then Grok CLI.
3. **codeart2d** only when the user explicitly asks for code-drawn art, or when `route_media.py` prints `no-route` (exit 3). Then say "code-drawn, no image model".

Every image goes through one command: `python "<skill-dir>/../generate2dmedia/scripts/route_media.py" image --prompt-file <txt> --reference <png> --size 1024x1024 --out-dir <new>`. It picks the route in that order and prints one JSON line with `route`, `artifact`, `sha256` and `estimateUsd`; `resolve --kind image` says which route would run. Existing art the user supplies is always welcome. Never claim a generation that did not happen; keep the prompt and contract and report a missing capability.

- **Finish:** HD is the default: full colour, clean alpha, one registration and a premultiplied area downscale to the game height. The pixel finish (one shared palette, binary alpha, no dither) is an option when the user asks for pixel art. Never prompt an image model for a logical pixel grid; the pixel look comes from the finish.
- **Characters:** approve one master still per character, then animate it with video2dsprite: one image-to-video clip per action, all from that master (`sprite_set.py`). Keep every action's body height from the master's rest pose.
- **Sheets** are for FX, icons and props (and frames the user supplies), not for character animation.

## Master still (every character animation starts here)

One approved master still per character, then one image-to-video clip per action from it ([video2dsprite](../video2dsprite/SKILL.md)); sheets are for FX, icons and props. Wording: [prompt-rules.md](references/prompt-rules.md); commands, framing and `master.json`: [master-still.md](references/master-still.md).

1. Spec (`--spec <json>` and/or flags): `--name`, `--subject`, `--identity` (long and precise), `--facing left|right|front|back`, `--class hero|mob|boss|prop`, `--finish hd|pixel` (HD default; pixel only when asked), `--identity-ref` (FIRST image, copied exactly), `--style-ref` (SECOND: an approved peer sprite, style and framing only), `--key` (default `#FF00FF`; green or blue for purple and pink designs).
2. Generate: `python "<skill-dir>/scripts/master_still.py" generate --spec <json> --takes 3 --output-dir <new>`. Each take goes through generate2dmedia `route_media.py` (API key first, then the local daemon); `takes.png` shows them side by side. Exit 3 means no route: say so; codeart2d is the fallback ("code-drawn, no image model"). With the host's own image tool: run `master_still.py prompt` and attach the listed references in that order.
3. Choose by looking: identity, facing, nothing touching an edge, no text.
4. Fix a near-miss by edit, never by re-roll: `master_still.py edit --run <dir> --take N --change "<one change>" --output-dir <new>` (or `--master <dir>`).
5. Approve: `master_still.py approve --run <dir> --take N --output-dir <new>` (a still from elsewhere: `--still <png> --route <what made it>`). It keys the still, crops it with a 6 px margin, LANCZOS-scales it to the class framing on 1024x1024 (hero 788 px tall, top margin 138 px) and writes `master.png` (opaque, pure key), `master_rgba.png` and `master.json` (`generate2dsprite.master.v1`).
6. Motion steps read `master.json` (`identity_recap`, `facing`, `finish`, `class`, `framing`, `key`, `files`, `transform`, `route`) and never re-measure the still; every clip prompt starts with "The same <identity_recap>".

Report the route from `master.json` `route`. A `FORGE_ROUTE_MEDIA_FAKE` warning means the image is not a real generation.

## Host notes

- **Codex:** `image_gen` is your own image tool; with no configured key use it and look at results with `view_image`. An image this session made that is not in the project: `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir <new>`, then process `generated.png`. codeart2d and generate2dmedia are explicit-only: open their SKILL.md when this one routes there.
- **Claude Code:** no built-in image generator: `route_media.py` takes the API or the local CLIs. Look at every PNG you make with Read; run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
- **Grok:** its native image and video tools are your own tools; use their real schema.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it (`<skill-dir>/../video2dsprite`). Keep inputs and outputs inside the project. Every tool writes a new `--output-dir`: it refuses an existing one, stages beside it and publishes only after its checks (`--strict-qc` / `--strict` publish nothing on failure). Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Needs Python 3.10+, numpy and Pillow (scipy recommended). `--help` lists every flag.

## Plan the asset

Record identity and reference, camera and facing, style, game display size, finish (HD by default, pixel on request), action list and phases, loop or one-shot, motion envelope, shared root and output format. Keep one accepted master and one source-to-display scale per character; mobs are no taller than the hero unless planned. A grid is not smoothness: pose spacing, timing, the loop seam and ground contact matter more. Poses, NEAR/FAR legs and timing: [animation-planning.md](references/animation-planning.md).

- Alpha: `native_alpha` keeps real transparency (a painted checkerboard is not alpha); `chroma_key` uses flat magenta only when the subject has none; `opaque` keeps whole images (`assemble_frames.py`).
- Sampling: `lanczos` or a premultiplied area resize for generated art; `nearest` only for a verified pixel grid. A high-resolution pixel-like painting is not clean 32 px art.
- A shared root is not a per-frame alpha-bbox bottom: keep intentional flight, jumps, bob and recoil.

## Generate (image routes)

Write the prompt yourself ([prompt-rules.md](references/prompt-rules.md); grids, phases and presets in [action-recipes.md](references/action-recipes.md)). Attach references through `--reference` (or your tool's real image input); a path in prose is not a reference. Keep the accepted master in every generation and describe absolute phases, not chained edits; a fix is an edit of the approved still that names the only change. Generate one action family at a time and keep wide slashes, trails and projectiles out of the body sheet. Verify the actual route, model and returned size; never claim them from the prompt.

## Pipeline

Sheets (FX, icons, props): `sheet_qc.py spill` on the raw sheet, then `generate2dsprite.py process`, then `sheet_qc.py frames`, then `scale_frames.py`, then `build_animation_clips.py`, then `export_engine.py`. Character actions come back from video2dsprite as packaged clips.

| Need | Route |
|---|---|
| Plan a sheet for an image tool (aspect, grid, guide, prompt) | `plan_guide.py --frames N --cycle run --output-dir <new>`; attach `guide.png`, paste `prompt.txt`. Fixed scale and feet line: `make_anchor_layout.py`; empty safe frame: `make_layout_guide.py` |
| Check a raw sheet before slicing | `sheet_qc.py spill --input <sheet> --rows R --cols C --output-dir <new>`; crossing parts mean regenerate with more margin or slice by ownership, never a largest-component filter |
| Key, slice and register a sheet | `generate2dsprite.py process --input <sheet> --target asset --mode <mode> --rows R --cols C --output-dir <new> --strict-qc` (geometry v2; [processing.md](references/processing.md)) |
| Pixel art drawn at M source px per art px | `process --resampler nearest --logical-pixel M --pixel-scale N` (whole scales only) |
| Jumps, bob or recoil on one registration point | `process --scale-strategy registered` (or `--anchor-px X,Y`) |
| One character's scale across actions | `process --write-scale-profile <p.json>` on the grounded reference, then `--scale-profile <p.json> --max-profile-scale-drift 0.08` |
| Canvas does not divide into the grid; other row order | `process --grid-rounding nearest` or `--pad-to-grid`; `--direction-order down,up,left,right` ([modes.md](references/modes.md)) |
| Identity, NEAR/FAR alternation, drift, baselines | `sheet_qc.py frames --sheet <sheet> --rows R --cols C --cycle run --output-dir <new>`; a duplicated half-cycle means regenerate the second half. On code-drawn frames (`art_source=code`) `identity_residual` and `torso_drift` are advisory |
| Game size on one canvas and root, never clamped | `scale_frames.py --frames <pngs> --root-lock torso-x --row-baseline --emit-clips --ticks N --output-dir <new>`; later actions add `--profile <first>/scale-frames.json --action-padding L,T,R,B` |
| Clips with timing, events, transitions | `build_animation_clips.py --manifest clips.json --output-dir <new>`; read the lints and `review/` ([frames-and-clips.md](references/frames-and-clips.md)) |
| Whole frames or a scene loop (no keying) | `assemble_frames.py` (`--key chroma`, `--slice ownership`; `--loop-overlap K --ambient` for ambient loops only) |
| Shared palette, locks, variants, flicker (pixel finish) | `palette_tool.py build`, `apply`, `lock`, `quantize-seq`, `variants`, `luts`; strict logical grid: `pixel_reduce.py` ([palette-and-pixels.md](references/palette-and-pixels.md)) |
| Engine export | `export_engine.py --clips <animation-clips.json> --target all --output-dir <new>`; Sprite3D: `--target godot-sprite3d --world-height <units>` ([engine-export.md](references/engine-export.md)) |
| Play clips in a game (gait, hit-stop, transitions) | [runtime-integration.md](references/runtime-integration.md) |
| Code-drawn sprites, rigs or FX, only on request | codeart2d `render_pixelspec.py`, `rig_animate.py` or `fx_build.py` with `--build-clips` (never `process`), then `export_engine.py`; disclose them as code-drawn |

## Acceptance

- Review at game size: silhouette and identity, contact and flight, loop seam or one-shot recovery, alpha over light and dark, extremities, scale across actions, root and collider.
- Quote numbers for quality claims: `pipeline-meta.json` `matte.qa` (a `key_ring_spill` warning: add `--despill-radius 1` or `--key-quality soft`), `sheet-qc.json`, the clip builder's lints. Numeric QC finds symptoms; it never approves anatomy or motion.
- Report every WARN or FAIL check verbatim (id, value, threshold, files) from each published QA envelope; never say "all checks passed" when any published envelope has a warn.
- Deliver accepted source art, runtime frames or clips, preview, prompt and provenance (route, model, `estimateUsd`), QA and remaining limits. Keep the last accepted bundle when revising.

## References

[processing.md](references/processing.md) (process flags, legacy switches), [modes.md](references/modes.md), [prompt-rules.md](references/prompt-rules.md), [action-recipes.md](references/action-recipes.md), [character-animation.md](references/character-animation.md) (animation index), [frames-and-clips.md](references/frames-and-clips.md), [palette-and-pixels.md](references/palette-and-pixels.md), [engine-export.md](references/engine-export.md), [runtime-integration.md](references/runtime-integration.md), [video-handoff.md](references/video-handoff.md); data contracts in `references/schemas/sprite.schema.json`.
