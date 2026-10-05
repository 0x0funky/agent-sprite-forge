---
name: generate2dsprite
description: Make game-ready 2D characters, creatures, props and FX as still sprites, sprite sheets or animation clips. Plan size, motion and art source; get or draw the art; then key, slice, register, scale, palette, QA and export the frames for a game engine (Aseprite JSON, Godot SpriteFrames or Sprite3D). Use for sprites, pose or action sheets, frame-by-frame animation, and packaging frames from any source. Small pixel sprites (48 px visible height or less) and game FX are drawn with codeart2d first. Not for maps, tiles or backgrounds (generate2dmap), image-to-video motion or video clips (video2dsprite), or calling an image or video API by itself (generate2dmedia).
---

# Generate 2D Sprite

Make the smallest asset bundle the game needs, at its real display size, camera and style.
Game logic (collision, damage, timing) stays independent of the art.

## Use when

- A character, creature, prop, item, icon or FX is needed as a still, a sheet or clips.
- Frames already exist (generated, painted, code-drawn) and need clips, palettes or an engine export.

## Do not use when

- Maps, tiles, terrain, parallax or scene plates: [generate2dmap](../generate2dmap/SKILL.md).
- Fluid organic motion (breathing, hair, cloth, creature attacks) or a video clip: accept one master here, then [video2dsprite](../video2dsprite/SKILL.md) ([video-handoff.md](references/video-handoff.md)).
- Only drawing code art: [codeart2d](../codeart2d/SKILL.md). Only an API or CLI call: [generate2dmedia](../generate2dmedia/SKILL.md).

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Use only the routes its `ROUTES` block names, in its order. `ready` means usable now: your own tool, or Codex / Grok (local CLI) VERIFIED for the installed version. `consent` (the paid API) needs the user's consent for each request. An installed CLI is not a connected tool until a verified run ([cli-routes.md](../generate2dmedia/references/cli-routes.md)). If `encoding.stdout` fails, set `PYTHONUTF8=1`.

## Art source

Choose per asset, record it as `art_source` (`code`, `host_image`, `api` or `existing`) and name the route in your reply. A route the user names explicitly wins.

- **Code-art envelope first, on every host** ([codeart2d](../codeart2d/SKILL.md), [style-envelope.md](../codeart2d/references/style-envelope.md)): pixel characters, props and palette variants up to 48 px visible height (49-64 px only with the user's consent), FX (slashes, sparks, rings, flashes, dust, projectiles), flat vector props, UI and icons. Above 64 px code art is not final art.
- **Other images, local agent first:**
  1. the host's own image tool (Codex `image_gen`, an image MCP);
  2. a local agent the doctor reports VERIFIED: Codex (local CLI) `image_gen`, then Grok (local CLI) one-shot image or edit, via `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" image --route auto --prompt-file <txt> --output-dir <new> --execute`. No per-call question within the session cap; always say which route ran;
  3. the paid REST API (generate2dmedia `generate_media.py`) only with the user's explicit consent for that request;
  4. existing art the user supplies;
  5. otherwise explain the gap and offer the paid API, the user's own art or a stylized code-art alternative.
- **Video:** Grok (local CLI) in ACP mode when VERIFIED, then REST with consent, then an existing clip (video2dsprite).

Never substitute silently. Code art is a declared route, not a placeholder: say "code-drawn, no image model" (`codeart-meta.json` records it) and never present it as image-model output. Never claim a generation that did not happen; keep the prompt and contract and report the missing capability.

## Host notes

- **Codex:** `image_gen` is the host image tool; look at results with `view_image`. An image this session made that is not in the project: `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir <new>`, then process `generated.png`. codeart2d and generate2dmedia are explicit-only: open their SKILL.md when this one routes there.
- **Claude Code:** no built-in image generator. Look at every PNG you make with Read; run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. Without an image tool or a VERIFIED local agent, use codeart2d inside its envelope and say so; otherwise explain the gap.
- **Grok:** its native image and video tools are the host tools; use their real schema.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it (`<skill-dir>/../codeart2d`). Keep inputs and outputs inside the project. Every tool writes a new `--output-dir`: it refuses an existing one, stages beside it and publishes only after its checks (`--strict-qc` / `--strict` publish nothing on failure). Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Needs Python 3.10+, numpy and Pillow (scipy recommended). `--help` lists every flag.

## Plan the asset

Record identity and reference, camera and facing, style, game display size (visible height decides the art source), action phases, loop or one-shot, motion envelope, shared root and output format. Keep one accepted master and one source-to-display scale per character. A grid is not smoothness: pose spacing, timing, the loop seam and ground contact matter more. Poses, NEAR/FAR legs and timing: [animation-planning.md](references/animation-planning.md).

- Alpha: `native_alpha` keeps real transparency (a painted checkerboard is not alpha); `chroma_key` uses flat magenta only when the subject has none; `opaque` keeps whole images (`assemble_frames.py`).
- Sampling: `nearest` for a verified pixel grid, `lanczos` for smooth art. A high-resolution pixel-like painting is not clean 32 px art.
- A shared root is not a per-frame alpha-bbox bottom: keep intentional flight, jumps, bob and recoil.

## Generate (image routes)

Write the prompt yourself ([prompt-rules.md](references/prompt-rules.md); grids, phases and presets in [action-recipes.md](references/action-recipes.md)). Attach references through the tool's real image input; a path in prose is not a reference. Keep the accepted master in every generation and describe absolute phases, not chained edits. Generate one action family at a time and keep wide slashes, trails and projectiles out of the body sheet. Verify the actual tool, model and returned size; never claim them from the prompt.

## Pipeline

Image sheets: `sheet_qc.py spill` on the raw sheet, then `generate2dsprite.py process`, then `sheet_qc.py frames`, then `scale_frames.py`, then `build_animation_clips.py`, then `export_engine.py`. Code-art frames skip process, sheet_qc and scale_frames: codeart2d `--build-clips`, then `export_engine.py`.

| Need | Route |
|---|---|
| Plan a sheet for an image tool (aspect, grid, guide, prompt) | `plan_guide.py --frames N --cycle run --output-dir <new>`; attach `guide.png`, paste `prompt.txt`. Fixed scale and feet line: `make_anchor_layout.py`; empty safe frame: `make_layout_guide.py` |
| Check a raw sheet before slicing | `sheet_qc.py spill --input <sheet> --rows R --cols C --output-dir <new>`; crossing parts mean regenerate with more margin or slice by ownership, never a largest-component filter |
| Key, slice and register a sheet | `generate2dsprite.py process --input <sheet> --target asset --mode <mode> --rows R --cols C --output-dir <new> --strict-qc` (geometry v2; [processing.md](references/processing.md)) |
| Pixel art drawn at M source px per art px | `process --resampler nearest --logical-pixel M --pixel-scale N` (whole scales only) |
| Jumps, bob or recoil on one registration point | `process --scale-strategy registered` (or `--anchor-px X,Y`) |
| One character's scale across actions | `process --write-scale-profile <p.json>` on the grounded reference, then `--scale-profile <p.json> --max-profile-scale-drift 0.08` |
| Canvas does not divide into the grid; other row order | `process --grid-rounding nearest` or `--pad-to-grid`; `--direction-order down,up,left,right` ([modes.md](references/modes.md)) |
| Identity, NEAR/FAR alternation, drift, baselines | `sheet_qc.py frames --sheet <sheet> --rows R --cols C --cycle run --output-dir <new>`; a duplicated half-cycle means regenerate the second half |
| Game size on one canvas and root, never clamped | `scale_frames.py --frames <pngs> --root-lock torso-x --row-baseline --emit-clips --ticks N --output-dir <new>`; later actions add `--profile <first>/scale-frames.json --action-padding L,T,R,B` |
| Clips with timing, events, transitions | `build_animation_clips.py --manifest clips.json --output-dir <new>`; read the lints and `review/` ([frames-and-clips.md](references/frames-and-clips.md)) |
| Whole frames or a scene loop (no keying) | `assemble_frames.py` (`--key chroma`, `--slice ownership`; `--loop-overlap K --ambient` for ambient loops only) |
| Shared palette, locks, variants, flicker | `palette_tool.py build`, `apply`, `lock`, `quantize-seq`, `variants`, `luts`; strict logical grid: `pixel_reduce.py` ([palette-and-pixels.md](references/palette-and-pixels.md)) |
| Engine export | `export_engine.py --clips <animation-clips.json> --target all --output-dir <new>`; Sprite3D: `--target godot-sprite3d --world-height <units>` ([engine-export.md](references/engine-export.md)) |
| Play clips in a game (gait, hit-stop, transitions) | [runtime-integration.md](references/runtime-integration.md) |
| Small pixel character, prop or palette variants | codeart2d `render_pixelspec.py --build-clips`; never `process` |
| Skeletal code-art character with planted feet | codeart2d `rig_animate.py --build-clips` (code-drawn; disclose it) |
| Game FX | codeart2d `fx_build.py --build-clips --export-runtime`; realistic fire, smoke or water: the video route |

## Acceptance

- Review at game size: silhouette and identity, contact and flight, loop seam or one-shot recovery, alpha over light and dark, extremities, scale across actions, root and collider.
- Quote numbers for quality claims: `pipeline-meta.json` `matte.qa` (a `key_ring_spill` warning: add `--despill-radius 1` or `--key-quality soft`), `sheet-qc.json`, the clip builder's lints. Numeric QC finds symptoms; it never approves anatomy or motion.
- Deliver accepted source art, runtime frames or clips, preview, prompt and provenance, QA and remaining limits. Keep the last accepted bundle when revising.

## References

[processing.md](references/processing.md) (process flags, legacy switches), [modes.md](references/modes.md), [prompt-rules.md](references/prompt-rules.md), [action-recipes.md](references/action-recipes.md), [character-animation.md](references/character-animation.md) (animation index), [frames-and-clips.md](references/frames-and-clips.md), [palette-and-pixels.md](references/palette-and-pixels.md), [engine-export.md](references/engine-export.md), [runtime-integration.md](references/runtime-integration.md), [video-handoff.md](references/video-handoff.md); data contracts in `references/schemas/sprite.schema.json`.
