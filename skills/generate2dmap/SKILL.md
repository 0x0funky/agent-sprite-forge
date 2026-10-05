---
name: generate2dmap
description: Plan and build 2D game maps and scenes - top-down and side-scrolling levels, tilemaps and autotiles, prop packs, parallax backgrounds and HD-2D battle or story plates - with collision, navigation and reachability checks, environment motion, a playable HTML preview and export to Tiled, Godot 4 or LDtk. Use for any map, level, room, scene, background, terrain or prop-kit request. Tile topology, autotiles, code-made layouts and stylized parallax are drawn with codeart2d first; painted scenery uses the image route. Not for characters, creatures, items or FX (generate2dsprite), image-to-video of characters (video2dsprite), or calling an image or video API by itself (generate2dmedia).
---

# Generate 2D Map

Build the smallest scene bundle the request needs, in the game's art, camera, renderer and data conventions.
A background picture needs no game system; a playable map needs explicit geometry and data beyond a painting.

## Use when

- A playable map, level, room set, tilemap, terrain or prop kit, with collision and exits.
- A parallax background, a battle, title or story backdrop, or an HD-2D plate with lights and motion.
- Map data must be validated, previewed or exported to Tiled, Godot or LDtk.

## Do not use when

- Actors, items and attack FX: [generate2dsprite](../generate2dsprite/SKILL.md). Character motion from video: [video2dsprite](../video2dsprite/SKILL.md).
- Only drawing code art: [codeart2d](../codeart2d/SKILL.md). Only an API or CLI call: [generate2dmedia](../generate2dmedia/SKILL.md).

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Use only the routes its `ROUTES` block names, in its order. `ready` means usable now: your own tool, or Codex / Grok (local CLI) VERIFIED for the installed version. `consent` (the paid API) needs the user's consent for each request. An installed CLI is not a connected tool until a verified run ([cli-routes.md](../generate2dmedia/references/cli-routes.md)). If `encoding.stdout` fails, set `PYTHONUTF8=1`.

## Art source

Choose per asset, record it as `art_source` (`code`, `host_image`, `api` or `existing`) and name the route in your reply. A route the user names explicitly wins.

- **Code-art envelope first, on every host** ([codeart2d](../codeart2d/SKILL.md)): tile topology and autotiles, layouts, collision, spawns and other map data, stylized parallax, small pixel props (up to 48 px visible height; 49-64 px with consent) and ambient FX. An image tool supplies only materials there, and re-skinned tiles must pass the seam proof again.
- **Painted scenery, local agent first** (HD-2D plates, painterly backgrounds, textured terrain, detailed props):
  1. the host's own image tool (Codex `image_gen`, an image MCP);
  2. a local agent the doctor reports VERIFIED: Codex (local CLI) `image_gen`, then Grok (local CLI) one-shot image or edit, via `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" image --route auto --prompt-file <txt> --output-dir <new> --execute`. No per-call question within the session cap; always say which route ran;
  3. the paid REST API (generate2dmedia `generate_media.py`) only with the user's explicit consent for that request;
  4. existing art the user supplies;
  5. otherwise explain the gap and offer the paid API, the user's own art or a stylized code-art alternative.
- **Video** (masked plate motion source): Grok (local CLI) in ACP mode when VERIFIED, then REST with consent, then an existing clip.

Never substitute silently. Code art is a declared route, not a placeholder: say "code-drawn, no image model" and never present it as image-model output. Never claim a generation that did not happen; keep the prompt and contract and report the missing capability.

## Host notes

- **Codex:** `image_gen` is the host image tool; look at results with `view_image`. An image this session made that is not in the project: `python "<skill-dir>/../generate2dmedia/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir <new>`. codeart2d and generate2dmedia are explicit-only: open their SKILL.md when this one routes there.
- **Claude Code:** no built-in image generator. Look at every PNG you make with Read (debug overlays, previews); run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. Without an image tool or a VERIFIED local agent, make tiles, layouts, collision and stylized parallax with codeart2d and say so; a painterly HD-2D scene still needs an image route, so explain the gap.
- **Grok:** its native image and video tools are the host tools; use their real schema.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it (`<skill-dir>/../codeart2d`). Keep inputs and outputs inside the project. Every tool writes a new `--output-dir` (or a new `--output` file): it refuses an existing one, stages beside it and publishes only after its checks (`--strict-qc` / `--strict` publish nothing on failure). Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Needs Python 3.10+, numpy and Pillow (scipy recommended); ffmpeg 5.1+ for scene motion; node for the browser runtime. `--help` lists every flag.

## Plan before art

Record viewport and logical resolution, world extent, origin, camera and zoom, actor scale and footprint, routes, landmarks, interactions, exits and render order. Source pixels are not world units: measure returned sizes and declare uniform scale and anchors. Choose by behaviour ([map-strategies.md](references/map-strategies.md); starting values per genre in [map-presets.md](references/map-presets.md)):

- `scene_mode`: authored area, town or arena; terrain plus props and explicit geometry.
- `tile_mode` / `grid_mode`: repeated or editable terrain or cell rules; tiles plus layer data.
- `room_chunk_mode`: connected rooms from a shared kit with door sockets.
- `side_scroll_mode`: side-view scenery or a playable platform stage with movement geometry.
- `baked_scene_mode`: a complete battle, title or story picture, with optional hotspots or bounded motion.

Split objects only when interaction, depth, reuse, editing or motion needs it. Pixel art needs deliberate logical pixels and nearest scaling; HD-2D pairs pixel actors with painted plates and light.

## Pipeline

Block out navigation, make art and kit, place it, validate the data, preview, then export. A playable map is a `map_bundle.v2` ([layered-map-contract.md](references/layered-map-contract.md)).

| Need | Route |
|---|---|
| Props from a sheet | `extract_prop_pack.py --input <sheet> --rows R --cols C --labels a,b --output-dir <new>` (`--grid-rounding nearest`, `--auto-boxes`, `--boxes-file <json> --keep-canvas`, `--world-scale 3/8`, `--suggest-footprint ellipse`); place by `anchor_px` ([prop-pack-contract.md](references/prop-pack-contract.md)) |
| Terrain fills, overlays, iso or hex, Wang rows | `extract_terrain_tiles.py --strict-qc` (`--layer overlay`, `--shape iso-diamond`, `--wang NAME=A/B:MASKS`; `--edge-policy seamless` only for art meant to tile) |
| Platform caps and middles | `extract_platform_strip.py --strict-qc` ([side-scroll-scenes.md](references/side-scroll-scenes.md)) |
| Tile topology, autotiles, collision from data | codeart2d `autotile_build.py` (seam-proven tileset manifest), then the exporters |
| A code-made playable map | codeart2d `layout_build.py` (writes map_bundle.v2), then `map_nav.py check` and the exporters |
| Draw order, contact anchors, placement audit | `compose_layered_preview.py --base <png> --placements <json> --output <png> --report <json>`; with `--bundle <map_bundle.json> --debug-overlay --audit-out <json>` look at both |
| Validate a playable map | `map_bundle.py validate --bundle <b>` (`map_bundle.py hash` fills sha256), then `map_nav.py check --bundle <b> --output-dir <new>` (`--link` each neighbour); review `nav-debug.png`; `map_nav.py query` tests one spot or move |
| Walk the map, check every route | `build_scene_preview.py --bundle <b> --output-dir <new> --verify`; attach `scene-snapshot.json` ([scene-preview.md](references/scene-preview.md)) |
| Export | `export_tiled.py export --bundle <b> --output-dir <new>` (`--embedded-variant` for Phaser); `export_godot.py --bundle <b> --output-dir <new>`; `export_ldtk.py --bundle <b> --output-dir <new>` ([engine-maps.md](references/engine-maps.md)) |
| Room chunks; side-scroll grammar | `validate_chunks.py --chunks <json> --output-dir <new>`; `validate_layout.py --layout <json> --output-dir <new>` |
| Parallax | `validate_parallax.py --spec <plan> --report <qa.json>` (declare `camera.pivot` and the `aspects` you ship); stylized layers: codeart2d `parallax_build.py` ([parallax-backgrounds.md](references/parallax-backgrounds.md)) |
| Fit a painting to the game size or floor | `conform_background.py conform --mode cover` or `--mode ground-fit`; subjects on every screen aspect: `conform_background.py validate-crops` |
| HD-2D plate | [hd2d-plates.md](references/hd2d-plates.md): plan `stage.json`, `scene_layout_guide.py`, generate with `guide.png` attached, then `validate_stage.py --stage <json> --output-dir <new>`; look at every `layout-*.png` |
| Lights, shadows, atmosphere | [hd2d-presentation.md](references/hd2d-presentation.md): `extract_scene_lights.py extract --stage <json>`, curate `lights.json` |
| Night, lit or damaged variant of a plate | `edit_locality_check.py --before <master> --after <variant> --stage <json> --edit-box <box>`; regenerate on failure |
| Water, fire, mist or cloth on a still plate | [background-scenes.md](references/background-scenes.md): `build_motion_mask.py --envelope-from <clip>`, `scene_motion.py build`, then `scene_motion.py qa` on the decoded file; code-made ripples or glow: codeart2d `ambient_bake.py` |
| Full-frame scene loop | generate2dsprite `assemble_frames.py` (`--static-regions`, `--loop-overlap K --ambient`) |
| Collision and paths in a web game | `references/runtime/map-runtime.mjs` (same rules as `map_nav.py`) |

## Acceptance

- A playable map is accepted only after `map_bundle.py validate` and `map_nav.py check` pass, and the `build_scene_preview.py` route check says `ok: true` for every route. map_nav proves reachability from data, not that collision matches the painted art.
- Compose with a representative actor at game scale; check spawn clearance, collider versus canopy, reciprocal portal arrivals, joins, repeat seams and camera coverage.
- With a plate the best stage status is `needs-visual-review`. Quote `scene_motion.py qa` numbers (decoded seam, frames, fps, keyint, bytes) before calling a loop seamless.
- Native alpha must keep purple; an RGB checkerboard is not transparency. Test devices before claiming mobile performance.
- Deliver originals and prompts, runtime assets and data, the preview and the checks run, with remaining limits.
- Report every WARN or FAIL check verbatim (id, value, threshold, files) from each published QA envelope; never say "all checks passed" when any published envelope has a warn.

## References

[map-strategies.md](references/map-strategies.md), [map-presets.md](references/map-presets.md), [layered-map-contract.md](references/layered-map-contract.md), [prop-pack-contract.md](references/prop-pack-contract.md), [side-scroll-scenes.md](references/side-scroll-scenes.md), [parallax-backgrounds.md](references/parallax-backgrounds.md), [background-scenes.md](references/background-scenes.md), [hd2d-plates.md](references/hd2d-plates.md), [hd2d-presentation.md](references/hd2d-presentation.md), [scene-preview.md](references/scene-preview.md), [engine-maps.md](references/engine-maps.md); data contracts in `references/schemas/map.schema.json`.
