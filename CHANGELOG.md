# Changelog

## 0.4.0 — 2026-10-06 — the big upgrade

One integrated release of the five skills (plugin version `0.4.0`). Not yet tagged; tagging and publishing are the owner's. Measured numbers come from the repository's fixtures and the 2026-10-06 live runs ([docs/validation-2026-10-06.md](./docs/validation-2026-10-06.md)); what the measurements do not prove is in [docs/known-limitations.md](./docs/known-limitations.md).

### Highlights

- **Image generation first, code art last.** Every generated image and clip goes through one command, `generate2dmedia/scripts/route_media.py`. It tries a configured API key first (OpenAI, Google Gemini, xAI, BytePlus ModelArk, fal.ai; a configured key is the owner's consent, so there is no per-call question), then the user's own signed-in CLI (the Codex CLI's `image_gen`; the Grok CLI's one-shot image or edit; Grok image-to-video in ACP mode), and only then answers `{"status":"no-route","fallback":"codeart2d"}` (exit 3). `codeart2d` draws only when the user asks for code-drawn art or no route exists, and the art is always disclosed as code-drawn.
- **One approved master still per character.** `generate2dsprite/scripts/master_still.py` (`prompt`, `generate --takes N`, `edit`, `pad`, `approve`) writes the prompt, generates takes through `route_media.py`, fixes a near-miss by edit ("THE ONLY CHANGE: ..."), pads the chosen still to the class framing (a hero is 788 px tall on 1024x1024) and writes `master.json`, which every motion step reads.
- **A whole sprite set from that still.** `video2dsprite/scripts/sprite_set.py` (`plan`, `run`, `review`, `accept`, `retake`, `report`) makes one image-to-video clip per action (idle, walk, run, attack, jump, hurt; cast, guard, victory, defeat on request), with per-action canvases (jump 3:4 with headroom, attack 16:9), numeric gates on every take, up to 3 automatic takes with fix clauses, soft-matte keying (one matte profile per character), registration, loop selection or one-shot retiming, the finish and verified packages. `run` resumes and never generates a clip twice; the agent looks at every review sheet before it accepts an action.
- **Finish: HD by default, pixel on request.** `finish_frames.py hd` keeps one scale per character from the master's rest pose (premultiplied area downscale on a grid pinned to the feet, clean alpha; never nearest, never an upscale). `finish_frames.py pixel` is bold pixel art: a crisp 4x4-cluster downscale, a light contrast and saturation lift, at most 32 colours from a spaced palette (no near-duplicate shades), lone-pixel cleanup that keeps eyes, a 1 px selective outline and binary alpha. The 48x64 fox run of the 2026-10-05 cold test, redone as master still → Grok clip → pixel finish: 594–709 colours per frame become 16–19, 199 alpha levels become 2, 732 lone pixels per frame become 75.
- **HD colour lock.** `colour_lock.py` and `finish_frames.py --colour-lock MASTER` hold every design colour on the master's in OKLab (chroma only, lightness kept, still pixels smoothed over time); `sprite_set.py` turns it on for every action. On Aria's run loop: hue flips 352 → 170 per frame pair, region chroma spread 0.0059 → 0.0012, boot chroma step 0.024 → 0.003.
- **One-shots retimed to game length.** Image-to-video plays a short action in slow motion. `retime.py --auto-oneshot` finds the motion's onset, holds and settle, cuts the frozen holds, pins the keys (the hit with a hit-stop, takeoff, land) and compresses the rest; `sprite_set.py` does it by default (attack 0.7 s with the hit at 40%, jump 0.9 s, hurt 0.5 s, cast 0.9 s). Aria: attack 4.1 s → 0.7 s (hit at 292 ms), jump 5.3 s → 0.9 s, hurt 3.9 s → 0.5 s.
- **QC recalibrated on real takes.** The gates were re-fitted on 42 labelled takes from the live run: identity only warns unless the design is lost, a colour-bleed gate, a timing gate for one-shots, the run's feet gate on a half-second median. Good takes wrongly rejected: 18 of 24 → 0 of 24 (precision 0.42 → 1.00; recall 0.72 → 0.50).
- **Five API providers behind one adapter layer.** `media_providers.py` with one capability record per model (`references/capabilities.json`): OpenAI GPT Image, Google Gemini, xAI Grok Imagine (image and video), BytePlus Seedream and Seedance, and fal.ai (Kling v3, Veo 3.1, Luma Ray, MiniMax, Wan, Vidu, LTX for video; four reference-edit image models).
- **Soft-matte keying, no purple fringe.** On the same Grok clip, old keyer vs 0.4.0: outer-ring fringe 0.704 → 0, opaque key pixels leaked 7,780 → 0, keying 271 s → 44 s for 145 frames.
- **Windows fixes.** Codex edits work on Windows (run folders the Codex sandbox can read), locked partial files are cleaned up with a retry, a CRLF checkout reads the prompt library, and every CLI runs under cp1252/cp950 consoles (details under Fixed).
- **No CI.** The repository ships no CI workflow; the suites run locally (`python -m pytest -q`, `node --test "tests/js/*.test.mjs"`, `python tools/vendor_sync.py --check --strict`, `python tools/check_links.py`).
- **Hosts.** Claude Code plugin (`.claude-plugin/`), plus the Codex and Grok folder installs with a backup and a drift check.

### Added

**Repository and contracts**
- Frozen JSON Schema 2020-12 contracts for all five skills in `shared/schemas/` (common, sprite, video, map, codeart, media), vendored into each skill's `references/schemas/` and validated by the standard-library evaluator `forge_schema`.
- Shared libraries with one canonical copy in `shared/`, vendored into the skills and kept byte-identical by `tools/vendor_sync.py` (sha256 test): `forge_core` (publication, image loading, components, alpha hygiene, anchors, premultiplied resampling, frame timing, seam reports), `forge_matte` (keyers), `forge_av` (ffmpeg probes and encoders), `forge_palette` (OKLab palettes), `forge_nav` (collision rules).
- Claude Code plugin: `.claude-plugin/plugin.json` and `marketplace.json`; `claude plugin validate --strict .` passes.
- `tools/install_skills.py` (backup, sha256 manifest, `--check` drift report, never ships bytecode) and `tools/check_links.py`.
- Real regression fixtures with `tests/fixtures/real/PROVENANCE.json`; pytest markers `perf`, `bench` (opt-in), `ffmpeg`, `node`, `resvg`, `e2e`; an end-to-end smoke over five pipelines and a JS/Python collision parity test.

**generate2dmedia**
- `route_media.py`: `image`, `video` and `resolve` over the route order above; `--route auto|api|local|<one route>|fal:<endpoint>`, `--model`, `--tier draft|standard|hero`, `--provider-option`, `--keyframe`, `--last-frame`, `--transparent`; a route that cannot take the request is skipped with the reason, and a request is adapted with a note (a video length snapped to one the model renders, a last frame the route cannot pin); account-level refusals pass to the next route, while a moderation refusal or an outcome that may have cost money stops at once. One ASCII JSON line (`status`, `route`, `artifact`, `sha256`, `estimateUsd`); `no-route` exits 3. `FORGE_ROUTE_MEDIA_FAKE` is the test seam.
- `media_providers.py` and `references/capabilities.json` (verified 2026-10-06 against the vendors' documentation): OpenAI (`gpt-image-2.5-sunburst` default, native transparent background, 16 references; `gpt-image-1` refused), Gemini (`gemini-3.1-flash-image`, `gemini-3-pro-image` for hero masters, 14 references), xAI (image edits with 5 references; `grok-imagine-video-1.5` with a last frame and up to 4 keyframes, the 1.5 Lite draft tier, actual cost from usage), BytePlus ModelArk (`ARK_API_KEY`: Seedream 5.0, Seedance 2.x with first and last frames), fal.ai (`FAL_KEY`: CDN uploads before the paid request, curated parameter maps for Kling v3, Veo 3.1 padded to 16:9 with the crop recorded, Luma Ray 3.2, MiniMax H3, Wan 3.0, Vidu Q3, LTX-2.5, and four reference-edit image models). Keys come from the vendors' variables or one user config file (`%APPDATA%\agent-sprite-forge\config.json`, `~/.config/agent-sprite-forge/config.json`) with an optional `providers.order` and per-tier `models`; they are read in-process only and never printed, logged, written or passed to a child process.
- `media_mp4.py`: every clip loses its audio locally before it is published; the download is kept as `provider-download.mp4`.
- `forge_doctor.py`: capability check with the Codex/Grok CLI readiness ladder (PRESENT, AUTH_MODE, TOOL_EXPOSED, VERIFIED) and a ROUTES block (each provider configured yes or no, local readiness, the resolved order); reads no credentials.
- `cli_media.py`: local routes `codex-cli` (image, up to 8 references), `grok-cli` (image, edit), `grok-acp` (image-to-video, 6 or 10 s), provenance checks, `resume --adopt`, `adopt --codex-thread` (supersedes draft PR #5; thanks to its author), `batch`.
- Spend ledger `.forge/ledger.jsonl` and `media_ledger.py`; opt-in caps `--budget-usd`, `--max-calls`, `FORGE_MAX_PAID_REQUESTS`, `FORGE_SESSION_IMAGES` / `FORGE_SESSION_VIDEOS` (no default caps); estimates from `references/prices.json`; job.json receipts; `batch` for paid jobs; `--base-url` with `--allow-custom-base-url`; `--upload-url` for xAI zero data retention (field name unverified).

**generate2dsprite**
- `master_still.py` (see Highlights): class framings for hero, mob, boss and prop; FIRST identity reference and SECOND style reference; fakes recorded and warned so a fake take is never mistaken for a real generation; `references/master-still.md`, `references/prompt-rules.md` rewritten around the recipe.
- `process`: `--scale-strategy registered`, `--pixel-scale` / `--logical-pixel`, `--grid-rounding nearest`, `--pad-to-grid`, `--anchor-mode`, `--alpha-hygiene`, `--key-quality auto|soft|hard|dominance`, `--key magenta|green|blue`, `--direction-order`, `--profile-override`; pipeline-meta `qa`, `provenance` and a `key_ring_spill` warning.
- `sheet_qc.py` (`spill` before slicing; `frames`: identity, NEAR/FAR alternation, drift), `scale_frames.py` (one scale and root, never clamps), `plan_guide.py`, `references/action-recipes.md`.
- `build_animation_clips.py` v2: 60 Hz ticks with drift-free integer ms, events, keys, entry frame, stride, transition hints, hit-stop, review sheets (filmstrips, onion skins, turn test, dissolves) and lints. `assemble_frames.py`: chroma keying, one crop-box schema, spill check and `--slice ownership`, `--loop-overlap K --ambient`.
- `palette_tool.py` (`build`, `apply`, `lock`, `luts`, `variants`, `quantize-seq` with temporal hysteresis: -46.4% noise flips on a real game clip) and `pixel_reduce.py`.
- `export_engine.py`: Aseprite JSON, Godot SpriteFrames and AnimatedSprite3D with an engine mapping table.

**video2dsprite**
- `sprite_set.py` (see Highlights) and `references/motion-prompts.md` (per-action prompt templates and a failure-to-fix clause library); `plan --matte-profile` keys every clip of a character the same way; a pixel character shares the scale-reference action's palette; `--fps` fractions and `--frame-ms` (12.5 fps gives exact 80 ms frames), `--colors`, `--outline`, `--canvas`, `--canvas-anchor`, `--no-colour-lock`, `--oneshot-timing auto|source`, `--oneshot-ms`.
- `finish_frames.py` (`hd`, `pixel`, `palette build`, `lineup`; see Highlights): `--scale-ref` for the other actions of a character (clamped to ±3%), `--display-sizes`, `--canvas WxH [--canvas-anchor X,Y]` fixed cells, `--indexed-sheet`, cast line-up sheets with size rules (hero, mob, boss, spirit), QA with flips per frame pair, specks removed and binary alpha. `--downscale box --contrast 1 --saturation 1 --outline none` gives the plain pixel finish; `--shade-gap 0` turns off the palette spacing.
- `colour_lock.py` (`measure` reports hue flips and region spread) and `finish_frames.py --colour-lock MASTER|rest`.
- `retime.py --auto-oneshot` with `--key NAME=SRC@F[+HOLDms]`, `--event-source` and fractional `--output-fps` (`25/2` gives exact 80 ms frames).
- Soft matte keying (`--matte soft|dominance|binary`), `--key auto`, pocket removal, `--despill-mode auto`, `--protect-color`, `--reference`, `--temporal-stability`, `--workers`, `matte-report.json`; `triage` and `key-plan` verbs; `--matte-profile`.
- `prepare_i2v_input.py` and `register_clip.py` (`apply`, `qc`, `profile`, `palette-repair`): registration by construction, padding contract, feet/x/hip locks, actor and fx profiles. `prepare_i2v_input prepare --master-key` keys an opaque master on a flat magenta, green or blue backdrop (recorded as `masterKeying`); `video2dsprite.py clean` also accepts `raw_*.png`.
- `gait_loop.py` (`select` with half-period guard, idle/hover loops; `measure-stride`), `retime.py` (spans, impact/hold, three-key map, ticks, loop-policy rules), `animation_review.py select`.
- `engine_export.py` CLI (`package`, `verify`, `doctor`): animation.json 3.0, mobile packed tiers, loop-aligned closed GOPs, hash-bound `verify-qa.json`. `validate_animation.py` with 27 rules.
- `references/runtime/forge-runtime.mjs` (distance-driven walk, hit-stop, transitions, fixed-step loop) and `packed-alpha-webgl.mjs` (WebGL compositor with CPU fallback).

**generate2dmap**
- `extract_prop_pack.py` v2 (anchors, footprints, auto boxes, keep-canvas, world scale, despill), terrain overlays, iso and hex tiles, Wang rows, border check; platform middle variants and surface QC.
- `compose_layered_preview.py` debug overlay, placement audit and plate pan; `validate_parallax.py` pivot, aspect sweep, pixel grid and seam verdicts; `conform_background.py`.
- `map_bundle.py` (`validate`, `hash`), `map_nav.py` (`check`, `query`), `export_tiled.py` (re-render verified at 0 px), `export_godot.py` (Godot 4.3+), `export_ldtk.py` (LDtk 1.5.3), `validate_chunks.py`, `validate_layout.py`, `references/map-presets.md`, `references/engine-maps.md`.
- `build_scene_preview.py` (single-file walkable preview with a route check) and `references/runtime/map-runtime.mjs` (mirrors forge_nav rules N1-N15).
- HD-2D: `validate_stage.py`, `scene_layout_guide.py`, `extract_scene_lights.py`, `edit_locality_check.py`, `build_motion_mask.py`, `scene_motion.py` (`build`, `qa`).

**codeart2d (new skill, the last-resort route)**
- `render_pixelspec.py` (PixelSpec to exact-palette frames per palette variant, clips manifests), `svg_render.py` (`render`, `lint`, renderer `doctor`), `pixel_qa.py`.
- `rig_animate.py`: SVG rigs with FK, two-bone IK and a ground constraint; 0 px planted-foot drift on the hero example.
- `fx_build.py` (slash, sparks, ring, flash, dust, projectile; hit events; `--export-runtime` writes an fx.v1 module) and `fx_verify.mjs`.
- `autotile_build.py`: Wang-16, three-material (81 tiles), blob-47, bevel and flat sets, each with an exhaustive seam proof.
- `layout_build.py` (playable map_bundle.v2 with collision and a reachability gate), `parallax_build.py`, `ambient_bake.py`; examples for every tool.
- `render_pixelspec` warns when a walk/run clip's half-cycle frames are near-duplicates (`half_cycle_duplicates`, silhouette IoU >= 0.95; override `--allow-duplicate-half-cycle`); the summary lists `warned_checks`.

### Changed

- Art routes (owner decision 2026-10-06): image generation is the default art source for characters, creatures, props, icons and FX; characters are animated from one master still with one image-to-video clip per action; sheets are for FX, icons and props. The five SKILL.md files and `agents/openai.yaml` files carry these rules, host notes for Claude Code, Codex and Grok, and a capability check. `codeart2d` and `generate2dmedia` are explicit-only in Codex.
- No default spend caps: a configured API key is consent, local routes run on the user's subscription quota, and every call is a ledger line; caps are opt-in. `generate_media.py` run directly keeps its dry run until `--execute`.
- The HD finish is the default everywhere; the pixel finish is chosen on request (`--finish pixel`, or a pixel master).
- `render_pixelspec --strict-qc` refuses only a `fail` status; a `warn` still publishes and is reported verbatim.
- Requirements: Python 3.10+, Pillow >= 10.1, numpy >= 1.26, scipy >= 1.11 (with a numpy fallback); `requirements-codeart.txt`; `requirements-dev.txt` adds jsonschema.
- Every CLI writes a new output folder through staged, no-replace publication, prints one ASCII JSON line, exits 1 on failure with one `error:` line and 2 on usage errors, and runs under cp1252/cp950 consoles. QA envelopes record `tool.version` 0.4.0. Manifests store relative paths; JSON inputs may carry a UTF-8 BOM.
- Speed: the legacy keyers are vectorised with identical bytes (2048^2 sheet: 22.0 s to 0.23 s); component labelling 2.5 s to 0.03-0.05 s per sheet; video keying 0.34 s per 960x960 frame on four threads.
- `video2dsprite` decodes and encodes through forge_av (functional ffmpeg probes, BT.709 tags, bitexact WebM, edge bleed under alpha).
- The package residue gate counts two-channel key spill only where the key channels stay balanced, so a deep crimson design colour no longer fails it while real magenta spill is still caught.
- Terrain and platform manifests are v2 (relative paths, QA envelopes); terrain tiles are RGBA. `--edge-policy seamless` now changes processing and gates wrap seams.
- `packed-alpha-runtime.js` wraps the WebGL compositor and keeps its API.
- `generate_media.py` prints scrubbed provider errors; image submit timeout 300 s.

### BREAKING

Each new default has a legacy switch where one is possible; v1 documents remain readable.

| Area | New default | Legacy switch |
| --- | --- | --- |
| sprite process | 8-connected components | `--connectivity 4` |
| sprite process | anchor = bottom edge of the lowest stable row | `--anchor-mode legacy-p98` |
| sprite process | alpha geometry threshold 16 | `--alpha-geometry-threshold 0` |
| sprite process | alpha hygiene `both` for native-alpha input | `--alpha-hygiene none` |
| sprite process | unknown `--mode` fails; `sheet` needs rows/cols | none (bug fix) |
| sprite process | profile/flag conflict is an error | `--profile-override` |
| sprite process | fractional nearest scales refused | `--legacy-fractional-nearest` |
| sprite process | `--key-quality auto` (soft for chroma + lanczos) | `--key-quality hard` |
| sprite process | pipeline-meta v2, scale profile v2 | v1 profiles still read |
| sprite process | `--align bottom` is a deprecated alias of `feet` | none |
| sprite process | `preserve` samples every cell on one shared grid (frames differ from 0.3) | none |
| sprite process | opaque multi-cell grids refused (use `assemble_frames.py`); `build-godot-bundle` refuses existing output | none |
| sprite build-prompt | no franchise style text | `--legacy-style` |
| sprite build-prompt | `--write` / `--write-json` refuse an existing file | `--overwrite` |
| assemble_frames | `--sheet` refuses cross-cell spill | `--allow-spill` (better: `--slice ownership`) |
| assemble_frames | `--key chroma` needs a backdrop of the key colour on the border | pass `--key-color` or drop `--key chroma` |
| clips tools | print one JSON line instead of the bare output path; relative paths | none |
| video process | `--matte soft` + pockets + `--despill-mode auto` + alpha hysteresis | `--matte binary --despill-mode off` |
| video process | `--dist` / `--despill` need `--matte binary` | none |
| video (all verbs) | refuse an existing output folder | choose a new folder |
| video frames | raw frames are `frame_000000.png` (0-based) | none |
| video package | fails on key residue | `--allow-key-residue` |
| video package | animation.json 3.0 (every 2.0 key kept); runtimes crop packed alpha at `halfWidth` | none |
| prop pack | refuses an existing output dir; exit 1 when nothing is accepted | new folder |
| prop pack | despill radius 1 for chroma | `--despill-radius 0` |
| prop pack | 8-connected; manifest v2 with `anchor_px` | `--connectivity 4`; v1 readable |
| prop pack | `--min-component-area` auto (scaled to the cell) | `--min-component-area 100` |
| compose | ground-line sort + manifest anchors | `--sort raw-y`, `--anchor px` |
| compose, parallax | refuse an existing output or report file | new path |
| terrain | runtime 3D fields omitted unless given | `--emit-runtime-defaults` |
| terrain | refuses an existing output dir; `--strict-qc` also fails drawn borders and art above a platform surface | new folder; `--max-border-delta 1`, `--surface-tolerance-px` |
| parallax | coverage required for repeated and near layers | `--coverage sky-only` |
| media | `generate_media.py` run directly refuses an identical successful request (`route_media.py` takes a new folder as a new take) | `--allow-duplicate` |
| media | job.json v2; image submit timeout 300 s | v1 jobs resume; `--submit-timeout 180` |
| repo | Python 3.10+, Pillow >= 10.1, scipy added, vendored `forge_*` in skills | none |

### Fixed

- Found by the second live run (2026-10-06, Aria HD set and fox pixel set): `sprite_set` passed the clip length as `6.0` where `route_media.py` takes whole seconds; it gave `finish_frames.py` both `--scale-ref` and `--target-height`; it had no per-character matte profile, so a crimson scarf on a magenta key left tinted boots and a package refused for spill; the old identity floor rejected 18 of 24 good takes; one-shots shipped at the generator's slow-motion speed (attack 4.1 s); the pixel finish kept 155-162 colours per frame and near-duplicate shades.
- Windows: every Codex master-still edit failed with ARTIFACT_MISSING, because `tempfile.mkdtemp` (Python 3.12.4+, CVE-2024-4030) gave the run folder an owner-only ACL that the Codex sandbox users cannot read; `cli_media.py` now makes the run folder with a plain mkdir in the system temporary folder, and ARTIFACT_MISSING quotes Codex's scrubbed answer. `forge_av` retries a partial-file cleanup for up to 3 s instead of failing with WinError 32 on a briefly locked file. `sprite_set` reads `motion-prompts.md` from a CRLF checkout. `forge_doctor` reads the Windows architecture from the environment instead of spawning cmd.exe.
- Grok renders 6 or 10 s only: other lengths failed as a bare GENERATION_FAILED. The `grok-acp` route snaps the request (`durationRequested`, `durationUsed`), and a failed ACP run carries the tool's own scrubbed message.
- Found by the first live validation: `prepare_i2v_input` refused an opaque master; the image-to-video prompt contract took the last noun of `--subject` ("everything behind the scarf"); an agent summarised a WARN QA envelope as "all checks passed" (SKILL.md now requires WARN/FAIL to be reported verbatim).
- Purple fringe and enclosed key pockets (report v2 P0-1, P0-2): on Ryo frames 57-87 visible fringe 8,504 to 0 px per frame, leak 15.3 to 0; frames with pockets 16 of 145 to 0; flicker 9.6 to 3.5 flips per frame pair.
- Packages shipped key residue (P0-3): residue is now refused; the old forge-cycle packages (70-83% ring spill) fail the gate.
- Invalid loop candidates (P0-4): six of eight old helper candidates on the report clip are listed as rejected with reasons.
- Body-scale pumping from alpha-bound registration; motion-killing prompt words are linted.
- Alpha haze drove geometry (S01, DOC-04: 67,907 faint pixels on the fox sheet removed); feet hung 2-3 px below the origin (S14); static-body shimmer (S05); uneven nearest pixels (S06); 16-bit grey turned white and animated input silently used frame 0 (S16); 22-29 s pure-Python keyer (S03).
- Props floated 0-8 px (MAP-02); magenta fringe on props 2,419 to 111 tinted px (MAP-03); raw-y sort drew short bushes over trees (MAP-05); banker's rounding moved props (MAP-21); outputs could overwrite inputs (MAP-22); absolute paths in manifests (MAP-24); hidden terrain runtime defaults (MAP-16, DOC-14); gutters passed strict QC (MAP-10); iso atlases rejected (MAP-11); seam metric failed true joins (MAP-14).
- No engine-usable map data (MAP-01, DOC-05, F-07): validated bundles, collision from data, reachability and Tiled/Godot/LDtk exports.
- A paid image was deleted on volumes without hard links (F-04); a paid success crashed a cp1252 console (F-14); provider error reasons were hidden (F-05, issue #11); the 180 s submit timeout was too short (F-16).
- Codex Desktop images that were not saved locally can be adopted (F-13, issue #4); stale skill installs are detected (F-11); an installed `grok.exe` is no longer mistaken for a video route.
- WSL `/mnt/c` publishing (F-03); `--help` crashed under cp1252 (report v2 P1-1); Pillow 12.3 wrote a corrupt GIF after a blank frame; libwebp 1.6 dropped the alpha flag of cropped previews; ffmpeg 8 left packed MP4 colour tags unspecified; WebM bytes changed on every run.
- Code-art probe findings: complex numbers and NaN in SVG geometry, clipPath id collisions between batched frames, outline gaps after finishing, 2.43 px foot slide, a 4 px shore colour jump between water tiles, and road-to-water sets that forced a grass strip.
- Sprite Sprite3D contract paths did not resolve (S19); Aseprite/Godot timing had no tool (DOC-13); odd-width packed videos were cropped at the wrong half.

## 0.3 and earlier

Grok Build and API video routes, packed-alpha video packaging and the 2026-10-05 audit fixes are described in [docs/upgrade-audit-2026-10-05.zh-TW.md](./docs/upgrade-audit-2026-10-05.zh-TW.md) and [docs/validation-2026-10-05.md](./docs/validation-2026-10-05.md).
