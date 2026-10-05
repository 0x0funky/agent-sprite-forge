---
name: generate2dmap
description: Generate and integrate 2D game maps, HD-2D scenes, scrolling backgrounds, tilemaps and terrain/prop kits. Plan gameplay geometry, camera, occlusion and connections first; produce reference-consistent artwork and optional animated environmental layers with runtime metadata.
---

# Generate 2D Map

Build the smallest scene bundle that satisfies the request. Preserve the game's art, camera, renderer and data conventions. A visual background does not require a game system; a playable map needs explicit geometry and behavior beyond a painting.

## Route by the scene

| Need | Read |
|---|---|
| Choose authored scene, tile/grid map or connected chunks | [map-strategies.md](references/map-strategies.md) |
| Exploration base, placed props, collision and occlusion | [layered-map-contract.md](references/layered-map-contract.md) |
| Compact prop packs, alpha and extraction | [prop-pack-contract.md](references/prop-pack-contract.md) |
| Platforms, structural joins and side-view movement | [side-scroll-scenes.md](references/side-scroll-scenes.md) |
| Real layered scrolling and camera coverage | [parallax-backgrounds.md](references/parallax-backgrounds.md) |
| Battle/story backgrounds, environment motion and mobile budgets | [background-scenes.md](references/background-scenes.md) |

Actor art and attack FX belong to [$generate2dsprite](../generate2dsprite/SKILL.md). Use [$video2dsprite](../video2dsprite/SKILL.md) for requested image-to-video animation and transparent-video processing, when available. For an explicitly chosen image/video API, use [$generate2dmedia](../generate2dmedia/SKILL.md). Do not duplicate provider clients here.

## Plan before art

Record viewport/logical resolution, world extent, origin, camera/zoom, actor scale and controller footprint. Identify routes, landmarks, interactions, entrances/exits and render order. Source pixels differ from display/world coordinates: measure returned dimensions and declare uniform scale and source anchors.

Choose from actual behavior, not genre alone:

- `scene_mode`: authored RPG area, town or arena; terrain plus props and explicit geometry.
- `tile_mode` / `grid_mode`: repeated/editable terrain or rules tied to cells; art plus native layer/object data.
- `room_chunk_mode`: connected authored rooms or modular layouts; shared kit, sockets and collision.
- `side_scroll_mode`: side-view scenery, or playable platform/brawler stage with movement geometry.
- `baked_scene_mode`: complete battle/title/story picture, optionally hotspots or bounded animation.

An RPG can use a large authored background, layered scene, tiles or chunks. A stable hand-painted room with a walkmesh is valid when its objects do not change or need walk-behind occlusion. Split objects when independent interaction, depth, reuse, editing or motion requires it; do not force distant trees/walls into separate assets. Choose stage/segment count from scope, not a mandatory two-screen default.

## Art source and continuity

Use the host's built-in image tool by default, existing suitable art, or the provider the user selects. API availability does not authorize an unexpected paid fallback. Inspect actual controls before promising dimensions, transparency or speed. Store prompts, reference roles, actual dimensions and returned provider/model provenance; a prompt-requested model is not proof of the model used.

View local references, then pass actual images through supported reference inputs. Reuse one accepted master for camera, palette, proportions and illumination. A path inside a prompt is not an attached reference. Preserve originals. Scripts may create guides, process pixels, compose QA and emit engine data; distinguish placeholders from final art.

Match style independently from camera. Pixel art needs deliberate logical pixels, nearest scaling and readable silhouettes. HD-2D can combine pixel actors with painted/3D scenery and light; it does not require shrinking detailed illustrations into tiny sprites or blurring everything.

## Production sequence

1. **Block out navigation.** Define walkable surfaces, collision bases, zones and exits. Battle scenes need reliable dry standing ground and space for commands/portraits.
2. **Generate stable art.** Keep independently changing or occluding objects addressable. A dressed concept pass is useful when composition is unresolved; it is optional for known layouts.
3. **Make a reusable kit.** Compact props can share sheets; buildings, broad canopies and structural joins need suitable canvases. Collision alone does not disqualify a compact prop pack. Preserve silhouettes and support anchors.
4. **Integrate behavior.** Store image rectangle, source anchor, world placement, collider, interaction reach and depth separately. Top-down Y sorting uses ground contact; platformers usually use stable render bands.
5. **Add selected motion.** Static scenery plus leaf/cloth/water/fire loops often works better than animating all geometry. Use video for requested organic motion, code particles/lights where useful, aligned posters and a finite active-video budget.
6. **Inspect gameplay scale.** Compose with a representative actor. Traverse routes, portals and camera extremes when integrating; JSON/screenshots do not prove navigation, parallax or video compatibility.

## Processing tools

Paths are relative to this skill directory; do not assume an installed host path.

| Script | Purpose / limits |
|---|---|
| `scripts/extract_prop_pack.py` | Exact grid or measured native boxes, auto/native alpha or magenta, opt-in alpha-noise floor, duplicate-label/edge checks; not structural strips |
| `scripts/compose_layered_preview.py` | Combines props/objects/actors/foreground; source `anchorPx`, nearest/Lanczos; no gameplay or video execution |
| `scripts/extract_terrain_tiles.py` | Opaque square terrain variants, explicit crop opt-in and QC; seamless intent is not proof |
| `scripts/extract_platform_strip.py` | Native cap/middle/end rectangles, contact-band and repeat-preview checks; no bbox fitting |
| `scripts/validate_parallax.py` | Actual alpha and camera/zoom canvas coverage; no seamlessness or finite-copy proof |

## Acceptance

Deliver originals/prompts, runtime assets/metadata, composed preview and checks performed. Include only route-relevant outputs; never pass a concept as separated runtime objects.

- Verify sizes, alpha, paths, anchors and resampling. Native alpha must retain purple; an RGB checkerboard is not transparency.
- Check spawn/path clearance, collider versus canopy, readable actors/interactions and reciprocal portal arrivals.
- For scrolling, inspect structural joins, independent depth motion, repeat copies/seams and camera coverage.
- For animation, verify meaningful motion, loop seam, registration, poster alignment, concurrency and fallback. Test actual devices before claiming mobile performance/alpha support.
- Separate asset/metadata checks from runtime observations. Preserve save/data contracts unless the requested integration needs a documented migration.
