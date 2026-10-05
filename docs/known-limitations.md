# Known limitations (0.4.0)

What the 0.4.0 tests and measurements do not prove. Consolidated from the module handoffs at release; each skill's references hold the detail for its tools.

## Everywhere

- **Platforms.** Development and every measurement ran on Windows 11 (Python 3.13, numpy 2.5, Pillow 12.3, scipy 1.18, ffmpeg 8.0.1, node 22). The release check also ran the suite at the Python 3.10 floor and without scipy ([validation](./validation-2026-10-06.md)). CI covers Ubuntu and Windows; macOS, and the Pillow 10.1 / numpy 1.26 floors, have not been run.
- **Determinism is per build.** PNG, GIF, WebP and video bytes repeat for one Pillow/zlib/libwebp/ffmpeg build; across builds, compare decoded pixels. Float32 math (soft matte, palettes, autotile noise) can differ in the last bit on other CPUs.
- **Numeric QA is not visual approval.** No check approves anatomy, identity, motion quality or appeal; every QA envelope lists these under `notProven`.
- **Publication fallback.** On filesystems without an atomic no-replace rename (WSL drvfs, some network mounts) a new output folder appears through an exclusive mkdir plus moves and can be seen half-populated. An existing destination is never replaced.

## Engines and players

- **Editor imports are not verified.** Godot 4.3+ (`export_godot.py`, `export_engine.py`), LDtk 1.5.3, Aseprite, Phaser, PixiJS and Unity outputs are verified at parse level only. Tiled output is verified by re-rendering the files (built-in reader and pytiled-parser), not in the Tiled GUI. Some Godot property names come from the Godot 4 source as remembered; LDtk required fields come from a hand-transcribed snapshot.
- **Not exported:** to Godot, material maps, nav grid, camera, stage, lights and animated parts; to LDtk, additionally walk regions and collision. Non-circular ellipse footprints become 32-gons.
- **Playback.** Only offline ffmpeg decoding is checked. The WebGL compositor ran in headless Edge/Chrome on SwiftShader only; no hardware GPU, Safari or iPhone run. Packed MP4 is not a transparent MP4.

## Sprites (generate2dsprite)

- Real-art evidence is one sheet (the fox fixture). Spill, near-duplicate, NCC drift and telegraph thresholds come from that sheet, synthetic tests and one game.
- The soft still key was tuned on one video clip and verified for stills on synthetic edges only.
- `preserve` keeps whole-pixel offsets, so the same subject at a different sub-pixel position in another cell samples differently; use `registered` or `--anchor-px` for registered art.
- Auto NEAR/FAR leg detection relies on the far leg being one shade darker; otherwise declare the colours. `plan_guide.py` is opt-in and not A/B tested with an image model.
- Palette hysteresis loses effectiveness under heavy noise (24-41% fewer flips at sigma 8); `pixel_reduce.py` handles integer grid periods only.
- `build_animation_clips.py` refuses indexed and 16-bit PNGs (convert first).

## Video (video2dsprite)

- **One clip.** The soft matte, the auto-despill rule, the 1% ring-spill gate and the loop thresholds come from the Ryo clip (magenta key, black-outlined cartoon, H.264 4:2:0). Green and blue keys, outline-free art, other codecs and fast motion are covered by synthetic tests only.
- Light purples close to a magenta key are keyed out unless protected (`--protect-color`, or a green key); narrow gaps filled with bright key glow stay opaque (about 170 key-hued px per Ryo frame); rims under a third coverage of a dark outline erode. Crimson, pink or purple outlines can count as key spill at the package gate.
- Flicker reaches 3.6 flips per frame pair on Ryo; the 2.0 stretch goal is not met.
- `register_clip.py --fit auto` assumes a centre crop when the provider changes the aspect ratio; a stretching provider needs `--fit stretch`. Take QC separates camera and subject motion only in calm spans, and misreads a loop that ends mid-stride as a push-in.
- `gait_loop.py` was measured on one real clip and synthetic runners; idle, hover and 12 fps content are synthetic only.
- Durations that are whole ticks of no supported rate stay PNG-only in packaging.
- The paid providers' prompt contracts follow earlier accepted takes; their effect on any provider was not measured in this release.

## Maps and HD-2D (generate2dmap)

- `map_nav.py` checks top-down movement only (no jump arcs); `one_way` blocks on every map because bundles carry no view mode, so top-down maps should not use it. A corridor narrower than `2 * actorRadius + cell` may be walkable but unprovable.
- Collision data is never compared with the painted art; look at `nav-debug.png`.
- Prop anchors and footprint suggestions are heuristics checked on six real props; lying or leaning props need an authored `anchor_px`.
- Terrain and platform seam thresholds are synthetic and not calibrated on image-model art; the border check uses luminance only.
- `validate_layout.py` treats the actor as a point at its feet; `validate_chunks.py` grids are top-down.
- HD-2D light extraction and the battle HUD profile are tuned on one game's plates; light lists need curation. Edit-locality thresholds are synthetic.
- Scene motion: a GOP-aligned encode alone does not pass the decoded-seam gate; the build uses a wrap-quality ladder. Thresholds rest on one project's loops. Loops are opaque H.264 only; provider zoom or rotation drift is not modelled.
- If an asymmetric object footprint is written already mirrored, forge_nav mirrors it again; `layout_build.py` footprints are symmetric today, so no shipped output is affected.

## Code art (codeart2d)

- The envelope is a design limit: pixel sprites up to 48 px visible height (49-64 px with consent). Pixel finishing shading is programmer-art level.
- Bit-exact SVG renders are pinned for resvg-py 0.5.0 on Windows; other backends and OSes use a tolerance. Chrome/Edge discovery on macOS and Linux is untested.
- Rig gates (L-corners, seam band, IK margins) were measured on one 64 px biped; IK is two bones on a horizontal ground line. FX loop seams are a warning; the fx.v1 runtime draws anti-aliased paths, not pixel-exact frames.
- Autotile repetition at or below 0.35 is claimed only for sets that report it; shared texture-noise bands (0.70) and image textures alone (0.92) do not meet it. Tilesets were not opened in Tiled, Godot or LDtk.
- `layout_build.py` accepts corner-Wang and flat tilesets (not blob-47 or bevel); two-material sets can cut a road that runs beside water (the reachability gate reports it). Exits are map-edge portals only.

## Media routes (generate2dmedia)

- No live provider call or live CLI run was made by the automated tests; the CLI recipes copy the owner's verified runs, and a route becomes usable only after its own verification on the installed version. Live results are recorded in the [validation](./validation-2026-10-06.md) document.
- Grok has no read-only sign-in status command, so its AUTH_MODE step stays UNKNOWN until a verified run.
- `prices.json` rows were transcribed, not re-fetched; OpenAI image models have no verified price row, so `--budget-usd` refuses them. The xAI `upload_url` field name is unverified.
- Paid caps are cumulative over the project ledger; the local session cap is a rolling window per project, not per conversation.
- `install_skills.py` copies every non-dot file of a skill folder, untracked files included; install from a clean checkout.
