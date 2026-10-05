# Side-view scenes and platform kits

Use this contract for a platformer, runner, side-view action room or scrolling stage. Solve the gameplay plane and its geometry first, then generate art that fits it. A small asset test needs one scenery image, a structural kit and a compact prop kit. For layered scrolling or richer depth, also read [parallax-backgrounds.md](parallax-backgrounds.md). A separate concept image is optional once the layout is resolved.

Commands run from the user's project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Write outputs into the project, never into the skill folder.

## 1. Stage plan template

Fill this in before generating art. Numbers come from the real controller when one exists; otherwise label the plan a provisional blockout, because metadata alone does not prove a stage is playable.

```text
viewport:        <W>x<H> logical px, zoom <z>, pixel snapping <on|off>
world:           <width>x<height> px, origin top-left, +y down
controller:      footprint <w>x<h> px, standing feet y, run <px/s>, jump height <px>, jump distance <px>,
                 step-up <px>, max slope <deg>, one-way platforms <yes|no>
camera:          bounds [x, y, w, h], look-ahead <px> in the travel direction, vertical dead zone <px> airborne
segments:        [x0, x1, kind] ... (ground, gap, platform, slope, arena, exit)
platforms:       collision rects [x, y, w, h], one-way flags, kit used for each
hazards/exits:   rects or points with their behaviour, entrances and spawn points
render bands:    sky, far, mid, rear decor, structure, actors and FX, foreground (section 6)
scenery layers:  file, source size, display scale, scroll factor, repeat interval
```

The `segments` and `physics` part is the map contract `layout_v1` in [map.schema.json](schemas/map.schema.json): `segments [[x0, x1, kind]]`, `physics {jumpHeight, jumpDistance, maxSlopeDeg, stepUp}` and `viewports`. The side-scroll layout validator (`validate_layout.py`, when present in this skill's `scripts/`) reads that file and checks floating props, deck thickness, slope and step limits, gaps against the jump arc, arena width at 16:9 and 19.5:9, and reachability. Use the real controller for anything it cannot judge.

Choose stage length from the task: one screen for a room or an asset test, two connected viewport widths for a scrolling sample. A brawler uses the engine's walkable belt and depth rules instead of platform jumps.

## 2. Viewport versus source pixels

The viewport is a gameplay contract; a generated image has whatever size the host returned. For each scenery layer and prop record source width and height, display size, one uniform scale, the world or screen anchor, the layer and the scroll factor. Never stretch art independently in x and y or resize everything to a nominal canvas. If aspect ratios differ, choose and disclose letterboxing, a reviewed crop or another source.

A non-repeating layer at zoom 1 with scroll factor `p`, camera travel `C` and viewport width `W` needs a displayed width of at least `W + p*C` plus a suitable start offset; check vertical coverage too. Repetition needs a tested seam and a declared repeat interval. [parallax-backgrounds.md](parallax-backgrounds.md) has the full transform and its validator.

## 3. The useful asset set

1. **Scenery.** One opaque backdrop for a static test, or an opaque sky plus real cut-out depth bands for layered scrolling. Keep the playable silhouette lane quiet; no solid-looking platforms, hazards, pickups or doors in scenery the runtime does not control.
2. **Structure.** A platform kit matched to authored collision: a left cap, one or more middle variants and a right cap with a shared surface line (section 5), or tiles. Never run structural strips through the prop-pack workflow, whose trim and bbox fitting break joins.
3. **Compact props.** A small side-elevation kit (chest, lamp, sign, crate) with authored collision. Wide machines, long pipes, large trees, exact-fit doors and landmarks may need individual assets.
4. **Assembly.** Compose these with a representative actor at gameplay camera size and fix scale, view, contact, seams and clutter before generating more art. For a playable character see [character-animation.md](../../generate2dsprite/references/character-animation.md).

## 4. Prompt templates

Platform kit (fill every `<...>`; ask for the background the extraction route expects):

```text
Create one platform kit for the SIDE-VIEW 2D game in the supplied reference: <left cap>, <N> interchangeable middle
sections and <right cap>, side by side in one row, each <w>x<h> px, at one camera and lighting scale.
All pieces share one walkable top line at <y> px from the top and the same body thickness and material. The right edge
of the left cap, both edges of every middle and the left edge of the right cap are joins: texture and the top line run
straight through them, with no outline, gutter, shadow or end cap at a join. Grass tips may rise at most <d> px above
the top line. Only the outer ends of the caps are rounded or outlined.
Background: <verified native transparency | pure flat #FF00FF>. No text, grid lines, guide marks or checkerboard.
```

Side-elevation props (a strict orthographic example, not a ban on a deliberately angled project style):

```text
Create one 2x2 sheet of four reusable static props for the SIDE-VIEW 2D game in the supplied reference.
Use the reference for palette, pixel cluster size, lighting direction, material language and gameplay scale; do not copy
its background. Camera: orthographic elevation, perpendicular to the gameplay plane; upright edges vertical, base
edges horizontal; no top-down lid, no isometric rotation, no vanishing point.
Cells in row-major order: <four object definitions and their gameplay roles>. Keep the size relationships:
<for example, chest below actor waist; lamp taller than chest>. Each object stays inside its cell with clear margin and
a readable base; no pedestal, ground tile, floor slab, detached particles or cast shadow unless requested.
Background: <verified native transparency | pure flat #FF00FF>. No text, labels, grid borders or checkerboard.
```

Inspect the returned PNG rather than assuming the prompt was followed. A fake checkerboard is not alpha: regenerate a clean chroma source instead of keying it.

## 5. Platform kits: extraction and QC

Measure the returned atlas, then write a spec. `source_box` is `[left, top, right, bottom)` in source pixels; `surface_y_px` and `collision_depth_px` are local to the crops. Give one `left_cap`, one or more `middle` variants (any middle may follow the left cap, any middle or itself, and precede the right cap) and one `right_cap`, all the same height. The optional `collision_span_px` `[x0, x1)` excludes outer cap padding; spans must still reach every join.

```json
{
  "surface_y_px": 32, "collision_depth_px": 64,
  "pieces": [
    {"id": "left", "role": "left_cap", "source_box": [0, 0, 256, 128], "collision_span_px": [8, 256]},
    {"id": "mid-a", "role": "middle", "source_box": [256, 0, 512, 128]},
    {"id": "mid-b", "role": "middle", "source_box": [512, 0, 768, 128]},
    {"id": "right", "role": "right_cap", "source_box": [768, 0, 1024, 128], "collision_span_px": [0, 248]}
  ]
}
```

    python "<skill-dir>/scripts/extract_platform_strip.py" --input art/platform-atlas.png --spec art/platform-spec.json --output-dir art/platform-kit-v1 --background-mode chroma_key --decoration-band-px 3 --max-seam-ratio 1.25 --strict-qc

The input may be RGB(A), a palette PNG with transparency, grey or grey+alpha. The output directory must be new: work is staged beside it and published only after QC, so a failed strict run leaves nothing behind. It writes `<id>.png` per piece (exact crops; RGB is zeroed where alpha is 0), `strip-preview.png` (left cap, the middles cycled to at least three, right cap) and `platform-strip.json` (`generate2dmap.platform_strip.v2`): manifest-relative paths with sha256, crops, `collision_rect_px`, `anchor_px`, band coverage, the measured surface, every join's metrics and a QA envelope. It never trims, resizes, normalises or repairs seams. Success prints one JSON line with the output and manifest paths.

| Flag | Default | Checks |
|---|---|---|
| `--background-mode` | `chroma_key` | `chroma_key` removes #FF00FF (the shared legacy keyer); `native_alpha` requires real transparency; `opaque` requires an opaque source. |
| `--despill-radius 0..3` | 0 | Chroma only: magenta excess removed within that many px of transparency. It can grey real purple at edges; inspect the result. |
| `--solid-alpha-threshold` | 255 | Alpha that counts as solid for the surface and collision band. |
| `--min-column-coverage` | 1.0 | Solid share of each collision-band column. Relaxing it never relaxes the solid surface row. |
| `--surface-tolerance-px` | 0 | How far the art's top may rise above `surface_y_px` in a collision column. More is a defect: actors would sink into the art. |
| `--decoration-band-px` | 0 | Rows directly above the surface that may hold non-structural tips (grass blades); ignored by the surface measurement. |
| `--max-seam-ratio` | off | Gates every join: a join step sharper than the art's own steps near it (ratio above the value; try 1.25), or a duplicated edge that stutters on every repeat, is a QC issue. Without it the verdicts are diagnostics. |
| `--max-seam-rgb-mae`, `--max-seam-alpha-mae` | off | Legacy raw-equality gates; they fail true seamless joins and pass duplicated edges, so prefer `--max-seam-ratio`. |
| `--strict-qc` | off | Publish nothing when any issue remains. |

The declared surface row must be solid in every collision column, so actors never stand on air. The art's top is measured per collision column: in `opaque` mode every pixel is solid, so the surface is recorded as not measured. Select the correct band rather than loosening coverage to hide a faulty platform.

Placement at uniform scale `s`:

```text
imageLeft = collisionLeft - s * collision_span_px[0]
imageTop  = collisionTop  - s * surface_y_px
```

`anchor_px = [0, surface_y_px]` is the visual left and contact top; with cap padding the collision left differs. Reuse one kit across segments, repeat middles at their own widths and test a real controller crossing every join; pixel snapping may still be needed.

## 6. Render bands and anchors

Platformers draw in stable bands, back to front. Do not reuse top-down dynamic y-sorting: a jump must not change which decor covers the actor.

| Band | Holds | Order inside the band | Collides |
|---|---|---|---|
| sky, far, mid | scenery layers with scroll factors below 1 | fixed layer order | never |
| rear decor | props behind the play plane (signs, lamps, foliage) | authored `sortY` | no, unless authored |
| structure | platform kits, tiles, walls, one-way decks | authored order | yes, from declared collision |
| actors and FX | player, enemies, pickups, projectiles, hit effects | spawn or z order | runtime bodies |
| foreground | occluders in front of the play plane (posts, leaves) | authored order | never |

With the generic composer, give each band explicit stable `sortY` values and put foreground items in its `foreground` array; that is preview ordering, not physics. An isolated prop's image rectangle, support point, collision shape and interaction reach are separate data: measure the support point in source pixels and use `anchorPx` for padded art. Floating decor uses a mounting anchor. A brawler with depth movement may sort by its ground line instead.

## 7. Camera notes

Patterns from a shipped side-view prototype; tune them against the real controller.

- Look ahead in the direction of travel, not facing, and only above a small speed, so turning to attack never sways the view; ease the offset (about 24 px) in.
- While grounded, let the vertical target follow the feet, so landing on a higher deck re-frames the view; while airborne, move it only by the overshoot past a dead zone (about 46 px), so jumps do not bob the view.
- Clamp the target to the world bounds, then ease toward it (about 0.14 per frame horizontally, 0.08 vertically), so the view slows into stage edges instead of stopping hard.
- Floor the camera to whole pixels for pixel art; scroll parallax layers from that integer position so layers never shimmer against each other.
- Check coverage at the camera start, middle and end and at the zoom limits: every layer must cover `W + p*C` (section 2).

## 8. Stage checklist

- Plan: viewport, world, controller numbers, segments and collision exist before final art; a `layout_v1` file passes the layout validator when it is available.
- Props: no unintended top-down lids, isometric sides, floor slabs, floating bases or tilted supports.
- Scale: important objects read at gameplay zoom next to the actor, not only on a contact sheet.
- Alpha: no key-colour fringe, fake checkerboard or eroded art; inspect over light and dark scenery.
- Contact: the kit passes `--strict-qc` (solid surface row, no art rising above the surface beyond the declared tolerance and decoration band); props sit on their supports; crops did not shift anchors.
- Joins: the strip preview shows no gap, doubled edge, phase jump or height step; with `--max-seam-ratio`, every join is continuous.
- Coverage: no exposed canvas at the camera start, middle, end and zoom limits; layered scenes really move at distinct speeds.
- Readability: hazards, pickups and exits stand out; decor does not look like a platform or hide the actor.
- Gameplay: with a runtime, actually cross every join, jump every gap, land on every platform and reach every exit. Static previews and JSON checks do not prove playability.

Report measured source and display sizes, extraction, anchor and join data, representative camera previews and the limits observed. Keep generation quality, pixel checks and engine playability as separate evidence.
