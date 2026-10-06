---
name: codeart2d
description: Explicit-only last resort. Draws game art from code, with no image model - pixel sprites from a PixelSpec, flat vector props, UI and icons from portable SVG, rigged characters with planted feet, FX with a JS runtime, seam-proven autotiles, playable top-down layouts with collision, stylized parallax and ambient plate motion - palette-exact, with QA and review sheets. Use only when the user explicitly asks for code-drawn art, or when generate2dmedia route_media.py finds no image or video route (no-route, exit 3). Never the default - image generation (generate2dsprite, generate2dmap, video2dsprite) comes first for characters, props, maps and FX. Not for painterly, detailed or likeness art, HD-2D plates or realistic fire, smoke or water. Always disclose the result as code-drawn.
---

# Code Art 2D

You write the art as text (PixelSpec JSON, portable SVG, rig, FX, material, layout or parallax
specs); the scripts render, check and package it. No image model is involved. It is a declared art
source (`art_source: "code"`), not a placeholder.

## Use when

- The user explicitly asks for code-drawn art: a PixelSpec or SVG sprite, palette variants drawn in code, a code rig, code FX, a seam-proven autotile set, a code-made layout, stylized parallax or ambient loops.
- `route_media.py` prints `no-route` (exit 3): no API key is configured and no local image or video route exists. Say so, and offer to add a key or a CLI as well.
- Pose and layout guides that help an image model (support material, not final art).

## Do not use when

- Any art request while an image or video route exists and the user did not ask for code art: [generate2dsprite](../generate2dsprite/SKILL.md), [generate2dmap](../generate2dmap/SKILL.md) or [video2dsprite](../video2dsprite/SKILL.md) generate it.
- Painterly, organic or identity-rich art, likenesses, portraits, HD-2D plates or textured terrain; realistic fire, smoke or water.
- Never trace licensed characters into specs.

## Capability check (once per session)

Run `python "<skill-dir>/../generate2dmedia/scripts/route_media.py" resolve --kind image` (or `--kind video`): when it names a route, the image or video skill takes the request unless the user asked for code art. `forge_doctor.py` in the same folder shows the full picture. If `encoding.stdout` fails, set `PYTHONUTF8=1`. For SVG work also run `svg_render.py doctor --report <new.json>` once per machine.

## Art source

This skill is the explicit-only last resort of the route order: the API when a key is configured, then the local image tools, then code art, only when the user asks for it or no route exists. Never substitute it silently for generated art. Before you draw, check what code art can and cannot draw well in [style-envelope.md](references/style-envelope.md) (its table describes strengths, not the route order), and tell the user when a request needs an image model. Disclose every result: "code-drawn, no image model" (`codeart-meta.json` records `"art_source": "code"`); never present it as image-model output.

## Host notes

- **Codex:** this skill is explicit-only; `image_gen` and `route_media.py` make the art unless the user asks for code art. Look at review sheets with `view_image`.
- **Claude Code:** use it only on request or after `no-route`. Look at every `preview-*.png` and `review/*.png` with Read before you report; run tools with Bash; `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
- **Grok:** runs the same Python tools; its native image tools make the art unless the user asks for code art.

## Commands

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. `<skill-dir>` is this skill's folder; sibling skills sit beside it (`--build-clips` calls `<skill-dir>/../generate2dsprite/scripts/build_animation_clips.py`). Keep specs and outputs inside the project. Every tool writes a new `--output-dir`: it refuses an existing one and publishes only a complete result; with `--strict-qc` a failed check publishes nothing. Success prints one JSON line; errors print `error: ...` and exit 1; usage errors exit 2. Examples to copy: `examples/`.

Dependencies: numpy and Pillow for everything; PixelSpec rendering and `pixel_qa.py` need nothing else. SVG art also needs one rasterizer: resvg-py (`python -m pip install "resvg-py>=0.5,<0.6"`, preferred), the resvg-js CLI on PATH, or Chrome/Edge (set `CHROME_PATH`) ([rasterizers.md](references/rasterizers.md)). Never PyMuPDF, skia-python or cairosvg: they silently ignore crispEdges, `<style>`, clipPath or gradients. `fx_verify.mjs` needs node.

## Hello sprite

1. Write `gem.pixelspec.json` (the "Hello sprite" block in [pixelspec.md](references/pixelspec.md)).
2. `python "<skill-dir>/scripts/render_pixelspec.py" --spec gem.pixelspec.json --output-dir out/gem-v1 --preview-scale 8 --strict-qc`
3. Open `out/gem-v1/preview-x8.png`; the frames are in `out/gem-v1/<variant>/frames/`.
4. Tell the user it is code-drawn (no image model).

## Pipeline

| Need | Route |
|---|---|
| Pixel character, prop or FX with palette variants | PixelSpec ([pixelspec.md](references/pixelspec.md)), then `render_pixelspec.py --spec <spec> --output-dir <new> --build-clips --strict-qc`; review with `pixel_qa.py` |
| Flat vector character, prop, UI or icon set | portable SVG with palette classes ([svg-profile.md](references/svg-profile.md)), then `svg_render.py render --svg <svg> --palette <palette.json> --output-dir <new> --zoom N` |
| Crisp pixel art drawn with SVG shapes | `svg_render.py render --crisp --zoom N --strict-qc` |
| Check an SVG before rendering | `svg_render.py lint --svg <svg> --profile portable` (add `--compile --palette <p>`) |
| Small character that walks, idles or attacks with planted feet | rig SVG plus a `rig_anim.v1` file ([rig-animation.md](references/rig-animation.md)), then `rig_animate.py --rig <svg> --anim <json> --output-dir <new> --build-clips --strict-qc`; look at `review/*.png` |
| Slash, sparks, impact ring, hit flash, dust, projectile | `fx.v1` spec ([fx-language.md](references/fx-language.md)), then `fx_build.py --spec <json> --output-dir <new> --build-clips --export-runtime`; baked frames for pixel games, `fx-runtime.mjs` for gameplay-timed FX ([fx-runtime-contract.md](references/fx-runtime-contract.md)) |
| Check an fx.v1 runtime | `node "<skill-dir>/scripts/fx_verify.mjs" <module.mjs> --report <file>` |
| Terrain transitions, roads meeting water, paths over ground, walls and ledges | material spec, then `autotile_build.py --material-spec <json> --output-dir <new> --strict-qc` (Wang-16, three materials, blob-47, bevel; [tiles-and-maps.md](references/tiles-and-maps.md)) |
| An image-tool texture inside exact tile masks | `autotile_build.py --material-texture NAME=PATH --quantize-textures` (one tile that wraps) |
| Playable top-down map: roads, props, exits, collision, reachability | `layout_build.py --spec <layout.json> --output-dir <new> --preview --strict-qc` (`--tiles <tileset manifest>` for real tiles); open `debug.png` ([layouts-and-parallax.md](references/layouts-and-parallax.md)) |
| Stylized parallax background | `parallax_build.py --spec <parallax.json> --output-dir <new> --validate --sweep-frames 49`; review `sweep-sheet.png` |
| Ambient motion on a static plate | `ambient_bake.py --plate <png> --spec <effects.json> --output-dir <new> --preview --strict-qc`; encode with generate2dmap `scene_motion.py` |
| QA any pixel frames | `pixel_qa.py --input "<glob>" --palette <palette, spec or codeart-meta.json> --review <new.png> --onion --strict --report <new.json>` |

Hand-offs: clips go to generate2dsprite `export_engine.py` (never `generate2dsprite.py process`); a layout's `map_bundle.v2` goes to generate2dmap `map_bundle.py validate`, `map_nav.py check` and `export_tiled.py` / `export_godot.py` / `export_ldtk.py`.

## Rules

- SVG (portable profile, enforced by lint): root width and height equal an integer viewBox (1 unit = 1 logical pixel); colours via `class="c-NAME"`; bones rotate with `transform="rotate(a cx cy)"`; no `<text>`, `<image>`, feTurbulence, feDisplacementMap or mix-blend-mode; pixel art adds `shape-rendering="crispEdges"` and no gradients, masks, filters or opacity.
- Tilesets are seam-proven before they are published: tiles assembled from the atlas are compared pixel for pixel with an independent global render over every adjacency the set allows. `--strict-qc` never passes without that proof.
- Fold empty FX tail frames before building clips; the builder refuses fully transparent frames.
- Side-view walk and run cycles: give the near and far legs a clear value contrast (the far leg two or more shades darker, never two darks that read alike) and keep the arms or forelegs visible (not hidden under a scarf, cape or bag). `render_pixelspec.py` warns `half_cycle_duplicates` when frame i and i+n/2 of a walk/run clip share a silhouette (IoU >= 0.95); fix the legs, or pass `--allow-duplicate-half-cycle` only after the review sheet shows the stride reads.
- generate2dsprite `sheet_qc.py frames` on code-drawn frames (`art_source=code`): `identity_residual` and `torso_drift` are advisory, since the head and root are fixed in code; judge them by eye, and treat `near_duplicates` and `leg_alternation` as real.

## Acceptance

Look at the preview and review sheets at game size and 1x before reporting ([style-envelope.md](references/style-envelope.md) has the review loop). Quote the QA status and failed checks from the JSON summary.
Report every WARN or FAIL check verbatim (id, value, threshold, files) from each published QA envelope; never say "all checks passed" when any published envelope has a warn. Deliver specs, rendered frames or tiles, clips or bundles, `codeart-meta.json` and the disclosure line. Data contracts: `references/schemas/codeart.schema.json`.
