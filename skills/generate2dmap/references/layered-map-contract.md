# Layered RPG map contract

A playable layered map is data, not a picture: a map bundle (`generate2dmap.map_bundle.v2`, see [schemas/map.schema.json](schemas/map.schema.json)) records the base art, tile layers, placed props, collision, portals, spawns, anchors and interactions. Three tools close the loop:

```bash
python "<skill-dir>/scripts/map_bundle.py" validate --bundle map/map-bundle.json --report map/qa/bundle-report.json
python "<skill-dir>/scripts/map_nav.py" check --bundle map/map-bundle.json --output-dir map/qa/nav
python "<skill-dir>/scripts/export_tiled.py" export --bundle map/map-bundle.json --output-dir map/tiled --embedded-variant
```

Commands run from the project root; outputs stay in the project. In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. Each `--output-dir` must be new; a failed check publishes nothing.

## Geometry and depth

Stable foundations can include terrain, roads and distant scenery. Keep independently animated or interactable props and walk-behind roofs or canopies separate. Buildings can use a base and door plus a roof occluder; trees need a small trunk footprint, never a collider covering the canopy.

Record source size and support anchor, display scale and position, ground footprint, interaction reach and state, depth baseline and optional motion source separately. Actor feet collide; the full sprite rectangle does not. Bridges need a walkable deck and reachable banks, not just art; canopies must not hide the approaches.

## Generation

Use known camera, style and terrain geometry. A separate concept is useful only when composition or identity remains unresolved. Show local references and attach their pixels; name fixed landmarks, path widths, light and the requested layer's content.

```text
Create the static foundation for this [camera/style] exploration scene.
Use the attached accepted master for palette, horizon and light direction.
Preserve [road, river, bridge anchor, entrances] from the layout. Terrain and
stable scenery only; leave [areas] for separate trees, doors and canopies.
Keep the travel corridor readable at [gameplay scale]. No UI, labels or actors.
```

A fixed scene without independent behavior can remain a complete picture. Do not remove distant scenery merely because it depicts buildings or trees.

## Image, collision and occlusion are separate

| Concern | Data in the bundle | Measured in | Rule |
|---|---|---|---|
| Image | the object's art (lookup order below), `anchor_px`, object `x`, `y`, `scale`, `flip_x` | prop pixels | the art is drawn so `anchor_px` lands on (`x`, `y`); `flip_x` mirrors it around that point |
| Collision | prop or object `footprint` `{shape, width, depth, offset, rotate, basis}`, `solid`; the `collision` block | prop pixels, scaled once (`basis` below) | trunk, base or legs only; canopies, leaves and flags never block |
| Occlusion | `occlusion_class`, `occupant_policy`, `occluder {alphaThreshold, source}` | alpha of the occluder image | opaque pixels hide an actor; transparent corners never do |

### Where an object's art comes from (D6)

Every reader of bundle objects (`map_bundle.py render`, `export_tiled.py`, the Godot and LDtk exporters, the scene preview and the runtime) looks an object's art up in this order; the first step that names an image decides:

1. `objects[].image`: the object's own art, relative to the bundle.
2. `props[prop]`: the registry entry's `image`, or, for an entry with `pack` and `label`, that accepted prop-pack item's image.
3. `prop_packs`: the first listed pack manifest with an accepted item whose `label` is the object's `prop`.
4. `occluder.source`.

A step that names a missing file is an error; it never falls through to the next step. `map_bundle.py validate` lists the objects without any art in one warning, and the render draws nothing for them. `anchor_px` is always the anchor in the unmirrored art.

`flip_x: true` mirrors the art around the anchor x: the mirrored image is drawn so that its point `[width - anchor_px[0], anchor_px[1]]` lands on (`x`, `y`). The footprint is mirrored with it (`offset[0]` and `rotate` change sign, rule N6 below), whether it comes from the object or from its prop, so write footprints unmirrored. `export_tiled.py` writes a flipped object with Tiled's horizontal-flip gid bit and a `flipX` property; a composed preview's placements mirror the same way. Mirror only art whose lighting allows it.

### Footprint basis (D7)

`footprint.basis` names the space a footprint is measured in:

- `prop_px` (the default): prop-image pixels, scaled exactly once by the instance `scale`. A footprint of `width` 8 on an object with `scale` 2 blocks a 16 px wide shape centred at `x + scale * offset[0]`, `y + scale * offset[1]`.
- `world_px`: world pixels, never scaled.
- `image_px`: the legacy name of `prop_px`. Readers accept it; writers write `prop_px`.

In a bundle any other basis is an error (compose warns and reads it as `prop_px`). Never add the actor radius to a footprint or a solid; the collision rules already sample the actor's own footprint. A rotated rect footprint becomes a polygon; `rotate` is in degrees, clockwise on screen (y down).

No alpha-fade occlusion: scenery stays fully opaque with alpha-tested cutouts. When an opaque occluder pixel actually covers an actor, draw a faint actor-shaped cue over it; do not fade or hide whole trees, and never trigger on the image rectangle. The only fade allowed is the board-only `rear_shift_and_fade` policy below.

## Occlusion class and occupant policy

These two enums are defined here once; prop packs (`occlusion_class`, `occupant_policy` in prop_pack.v2) and map bundles use the same values.

| `occlusion_class` | Meaning | Typical policy |
|---|---|---|
| `low` | below an actor's chest: rocks, stumps, crates, flower beds | `y_sort` or `static_back` |
| `tall` | can hide a standing actor: trees, lamps, columns, statues | `y_sort` plus the occluder cue |
| `foreground` | overhangs the walkway near the camera: eaves, arches, canopies | `static_front` |

| `occupant_policy` | Use it for | Runtime behaviour |
|---|---|---|
| `y_sort` (default) | free-roaming exploration | drawn with actors in ground-line order (render order below) |
| `rear_shift_and_fade` | fixed boards and grids only (tactics, isometric boards) where an occupant stands on a cell with tall walkable dressing | while the cell is occupied or selected, shift the dressing toward the cell's rear edge and fade only its tall part, with hysteresis; the ground texture never fades |
| `static_front` | foreground occluders | always drawn after actors |
| `static_back` | floor decals, rugs, shadows, low clutter | always drawn before actors |

Any other value is reported by `map_bundle.py validate` as a warning.

## Render order

1. Image and tile layers in bundle `layers` order (for example `ground`, then `decoration`).
2. Each objects layer: props and actors together, sorted by `sortY` (the ground line: the anchor's world y unless authored otherwise), then x, then id. Actors sort by their feet.
3. `static_front` and `foreground` props after every actor; `static_back` props before them.
4. Effects, UI and debug overlays.

Rule 3 is a runtime duty: no tool here reorders objects by policy. `map_bundle.py render` draws each objects layer in rule 2 order (it proves what the data says, not how an engine sorts), and `export_tiled.py` writes the objects in that order with `occlusion`, `occupantPolicy` and `sortY` properties for the engine to split into its passes. A composed preview gets rule 3 from the placements' `foreground` array.

Tiled's own `topdown` object order sorts by the image bottom, which differs from the ground line whenever the anchor is not on the image's bottom row. `export_tiled.py` therefore writes prop objects already sorted with `draworder: index` and a `sortY` property; engines should sort by `sortY` at run time. Platformers usually need stable render bands rather than sort changes while jumping (see [side-scroll-scenes.md](side-scroll-scenes.md)).

## Placement for a composed preview

`compose_layered_preview.py` draws props, objects, actors and foreground over a base for review. Placements name the image, the world position and the anchor.

Gameplay metadata lives in the map bundle: it is the only collision, occlusion and policy data that `map_nav.py`, the exporters and a runtime read. A placement's own `footprint` and `solid` are preview data: they let the audit judge a composed preview that has no bundle, and the debug overlay draws them. With `--bundle`, actor feet are judged on the bundle's blocking set through forge_nav, exactly as `map_nav.py query` judges them (D4); a bundle forge_nav cannot read stops compose with an error (run `map_bundle.py validate` first), and a bundle without a `collision` block is judged with a point actor (`actorRadius` 0). Placement footprints then block nothing, and an actor standing inside one is reported by the `actor_feet_off_placement_footprints` warning, usually a prop that is missing from the bundle. Placement footprints use the same `basis` names (`prop_px` follows the drawn sprite, `world_px` only `--scale`) and mirror with a placement's `flip_x`.

```json
{
  "schema": "generate2dmap.placements.v2",
  "props": [{"id": "tree-01", "image": "../props/tree/prop.png", "x": 420, "y": 512,
             "anchor": "px", "anchorPx": [128, 370], "sortY": 512, "layer": "props"}],
  "actors": [{"id": "player-preview", "image": "../actors/player.png", "x": 460, "y": 530,
              "anchor": "px", "anchorPx": [32, 62], "layer": "actors"}],
  "foreground": []
}
```

```bash
python "<skill-dir>/scripts/compose_layered_preview.py" --base map-base.png --placements map-placements.json --output assembled.png --resampler nearest --report assembled-report.json --bundle map/map-bundle.json --audit-out assembled-audit.json
```

`anchorPx` is in source pixels. Foreground draws last. A v1 prop pack anchors its props at the art's bottom centre (`extract_prop_pack.read_manifest`). Inspect at gameplay scale with an actor in front of, behind and beside tall props. A PNG verifies composition, not gameplay, occlusion cues or video readiness.

## Collision JSON

The same object, as gameplay data in the map bundle, with a mirrored second tree:

```json
{
  "props": {"tree": {"image": "props/tree/prop.png", "anchor_px": [128, 370],
                     "footprint": {"shape": "ellipse", "width": 32, "depth": 18, "offset": [4, 0], "basis": "prop_px"},
                     "solid": true, "occlusion_class": "tall", "occupant_policy": "y_sort"}},
  "objects": [{"id": "tree-01", "prop": "tree", "x": 420, "y": 512, "scale": 0.5, "anchor_px": [128, 370],
               "sortY": 512, "occlusion": "tall",
               "occluder": {"alphaThreshold": 16, "source": "props/tree/prop.png"}},
              {"id": "tree-02", "prop": "tree", "x": 700, "y": 470, "scale": 0.5, "anchor_px": [128, 370],
               "flip_x": true}],
  "collision": {
    "actorRadius": 6, "ySquash": 1.0,
    "walkRegions": [{"polygon": [[0, 0], [960, 0], [960, 640], [0, 640]],
                     "holes": [[[300, 200], [340, 200], [340, 240], [300, 240]]]}],
    "solids": [{"shape": "rect", "x": 100, "y": 100, "w": 32, "h": 16},
               {"shape": "ellipse", "cx": 600, "cy": 300, "rx": 16, "ry": 9, "rotate": 20},
               {"shape": "polygon", "points": [[200, 300], [240, 300], [220, 330]]}],
    "rects": [[0, 620, 960, 20]]
  },
  "material_map": {"image": "materials.png", "materials": {
    "grass": {"class": "decor", "color": "#4e9640"}, "water": {"class": "liquid", "color": "#2c62ab"},
    "shallows": {"class": "liquid", "color": "#5a8fd0", "walkable": true}, "boulder": {"class": "solid", "color": "#7a7a7a"}}}
}
```

`tree-01` blocks an ellipse 16 x 9 px centred at (422, 512); the mirrored `tree-02` blocks one centred at (698, 470).

## Collision rules

`forge_nav.py` (canonical copy `shared/forge_nav.py`, vendored into this skill's `scripts/` beside the `forge_core.py` it needs) is the one implementation of the map collision rules. `map_nav.py`, the compose audit with `--bundle`, the Tiled, Godot and LDtk exporters, the codeart layout builder and the scene preview all read a bundle's blocking set through it, and [runtime/map-runtime.mjs](runtime/map-runtime.mjs), the JavaScript collision query that the playable scene preview ([scene-preview.md](scene-preview.md)) inlines, mirrors it rule for rule. Its module docstring is the complete rule book (N1 to N15, with the exact arithmetic). These are its rules for the walk area, the blocking set, solids, footprints, materials and one-way platforms, quoted verbatim:

```text
N3  Walk area. Without walk regions it is the closed box 0 <= x <= W and 0 <= y <= H.
    With walk regions a point is in the walk area when some region's polygon contains it
    and none of that same region's holes contains it. Containment is the even-odd crossing
    test (pnpoly): start outside; for i = 0 .. n-1 take a = poly[i] and b = poly[i - 1]
    (i = 0 pairs with the last vertex); skip the edge when a.y == b.y; otherwise toggle when
    ((a.y > y) != (b.y > y)) and x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x. Left and
    top boundaries count as inside and right and bottom boundaries as outside, so regions
    sharing an edge leave no seam; for a hole the same rule means its left and top
    boundaries are not walkable and its right and bottom boundaries are. Membership is
    decided for each sample separately. Regions are never inflated or shrunk.

N4  The blocking set (D2) is identical for every consumer. It is the union of:
      a. collision.solids, in world px;
      b. collision.rects [x, y, w, h], each the closed rect solid (D3: exact blocking
         rectangles, never an approximation of something else);
      c. the footprints of objects whose solid is not false (N6);
      d. the per-tile collision of placed tiles (N7);
      e. the material-map pixels of a blocking class (N8).
    A shape without area blocks nothing and is dropped, wherever it comes from: a rect with
    w <= 0 or h <= 0, an ellipse with rx <= 0 or ry <= 0, a polygon whose shoelace sum
    sum(x[i] * y[i + 1]) - sum(y[i] * x[i + 1]) is exactly 0 (map_bundle.py refuses such a
    polygon outright in collision.solids and in a tileset's tiles[].collision, where it also
    refuses a self-intersecting ring). A point is blocked when it lies in any member. Walk regions
    (N3) bound the walk area; they are not blockers.

N5  Solids are closed sets (D1); a point on a solid's boundary is blocked.
      rect x, y, w, h:   x <= px <= x + w and y <= py <= y + h (x + w, y + h computed first)
      ellipse cx, cy, rx, ry, rotate:  ex = px - cx, ey = py - cy; u = ex, v = ey when the
         rotation is 0, else u = ex * c + ey * s and v = ey * c - ex * s; then
         nu = u / rx, nv = v / ry and the point is blocked when nu * nu + nv * nv <= 1
      polygon points:    the even-odd test of N3, OR the point lies on an edge: for an edge
         a -> b, (b.x - a.x) * (py - a.y) - (b.y - a.y) * (px - a.x) == 0 and
         min(a.x, b.x) <= px <= max(a.x, b.x) and min(a.y, b.y) <= py <= max(a.y, b.y).
         (Exact for axis-aligned edges and vertices; a point on a slanted edge counts when
         that product rounds to zero.)
    Solids are never inflated by the actor size: the samples of N2 add it, once.

N6  Object footprints (D2, D6, D7). An object's footprint and solid come from the object,
    else from its props-registry entry (bundle.props[prop]: an inline item, or the
    accepted prop-pack item named by pack + label, the entry's own fields overriding the
    pack item's). prop_packs, the art lookup of D6 step 3, do not supply footprints. When
    solid is given neither way, the object is solid exactly when its footprint shape is
    ellipse or rect. A solid object with an ellipse or rect footprint blocks this shape:
      k = 1 when footprint.basis is "world_px", else the instance scale (objects[].scale,
         default 1); basis "prop_px" (canonical) and its legacy alias "image_px" are
         measured in prop-image pixels and scaled once (D7); any other basis is an error;
      (ox, oy) = footprint.offset (default [0, 0]); rot = footprint.rotate (default 0);
      flip_x true mirrors the footprint around the anchor x (D6): ox = -ox and rot = -rot;
      cx = x + k * ox, cy = y + k * oy, w = k * width, h = k * depth;
      ellipse:  centre (cx, cy), rx = w / 2, ry = h / 2, rotate rot;
      rect, rot 0:  the rect (cx - w / 2, cy - h / 2, w, h);
      rect, rot != 0:  the polygon of the corners (u, v) = (-w / 2, -h / 2), (w / 2, -h / 2),
         (w / 2, h / 2), (-w / 2, h / 2), each at (cx + u * c - v * s, cy + u * s + v * c).
    Footprints are never inflated by the actor radius.

N8  Material map. The image covers the world in squares of s = W / image width px; s must
    be a whole number >= 1 with s * image height == H. A point (x, y) reads the pixel
    (floor(x / s), floor(y / s)) when 0 <= floor(x / s) < image width and
    0 <= floor(y / s) < image height; outside the image there is no material. A pixel
    therefore covers [mx * s, (mx + 1) * s) x [my * s, (my + 1) * s): material squares are
    half-open (their right and bottom edges belong to the next pixel), unlike solids.
    Colour images (decoded to 8-bit straight RGBA): a pixel with alpha 0 has no material;
    otherwise its RGB must equal exactly one material's color. A P or L image whose
    materials all have an index matches pixel values to indices. A pixel that matches no
    material refuses the bundle. Classes: solid blocks (even with walkable: true); liquid
    and hazard block unless walkable is true; decor never blocks; one_way never blocks a
    point (N11).

N11 one_way (D2: blocks from above only; a side-scroll class). A move with b.y > a.y
    (moving down) is blocked when, between consecutive samples k - 1 and k of N10, any of
    the 9 footprint samples goes from a pixel that is not one_way onto a one_way pixel;
    with the thin-gap rule the centre also may not enter one_way along a, the piece
    midpoints of N10 and b. Upward and sideways moves always pass (jump up through a
    platform, stand on it). Bundles carry no view mode, so the rule applies to every map:
    a top-down map should not use one_way.
```

In short, for the rest of the rule book:

- World pixels, y down (N1). The actor's footprint is an ellipse `rx = actorRadius`, `ry = actorRadius * ySquash`; `ySquash` is 1.0 for top-down tiles and about 0.58 for HD-2D plates. A position is valid when its centre and 8 points on that ellipse are all in the walk area and none is blocked (N2, N9).
- `collision.rects` are exact blocking rectangles (D3), never an approximation of something else. A writer may emit rects that approximate its other solids only if its own reachability proof runs on the full blocking set, which forge_nav gives it.
- Tile collision (N7, D5): a placed tile blocks with its tileset tile's `collision` shapes (tile pixels), or its whole cell when it has no shapes and `properties.walkable` is false. `export_tiled.py` keeps these shapes in the TSX tilesets.
- Navigation (N10, N12 to N14) uses a grid of `max(1, round(actorRadius / 2))` px. A move between neighbouring cells is open when every sample along it is valid and the actor's centre never crosses a blocker or leaves the walk area, so walls and gaps thinner than a cell are never jumped; touching a vertex, a corner or an ellipse at a single point is not crossing it. A corridor must be wider than `2 * actorRadius + cell` px to be sure to show up on the grid.
- `one_way` is a side-scroll class (N11, D2): it blocks only moves that drop onto it from above. Bundles carry no view mode, so the rule applies everywhere; top-down maps should not use it.
- Portal intent, `latch` and `requiresMovement` are runtime duties (N15).

## QA checklist

- `map_bundle.py validate`: contract, files and sha256, unique ids, tile indices, wang and blob data, portal targets, slots, the art of every object (D6), anchors outside the art and object anchors that differ from their prop's (the prop's footprint would move against the art).
- `map_nav.py check`: every interaction, exit, anchor slot and approach point reachable from the spawns and arrivals; arrivals outside every trigger; triggers inside the world; links in both directions with `--link other-map.json`. Review `nav-debug.png`: red cells blocked, green reachable, yellow walkable but unreachable, orange thin gaps.
- No collider is the size of a canopy or a whole image; every solid prop has a footprint; footprints are scaled once and have a known `basis`.
- Tall props on walkways have an occlusion class and an occupant policy; the occluder cue works over opaque pixels only; the runtime draws `static_front` and `static_back` in their own passes.
- Compose a preview with a representative actor at gameplay scale, with `--bundle` so the audit judges feet on the bundle's collision.
- Export with `export_tiled.py` when the target loads Tiled data; the export proves the re-rendered files match the bundle, not that the Tiled editor or an engine imports them (Tiled GUI not verified).
- Walk the routes in the actual runtime: in front of and behind props, around corners, over bridges and through portals both ways.

Strategy-level guidance (routes, portals, autotiles, chunks, roads and bridges) is in [map-strategies.md](map-strategies.md); genre presets and engine targets are in [map-presets.md](map-presets.md); prop extraction and anchors are in [prop-pack-contract.md](prop-pack-contract.md); walking a bundle in a browser before exporting it is in [scene-preview.md](scene-preview.md).
