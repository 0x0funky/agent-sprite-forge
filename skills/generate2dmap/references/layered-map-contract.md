# Layered RPG map contract

## Geometry and depth

Stable foundations can include terrain, roads and distant scenery. Keep independently animated/interactable props and walk-behind roofs/canopies separate. Buildings can use a base/door plus roof occlusion; trees need a small trunk collider, not a collider covering the whole canopy.

Represent source size/support anchor, display scale/position, blocker/walkmesh, interaction reach/state, depth baseline and optional motion source separately. Actor feet/capsule collide; the full sprite rectangle does not. Inflate blockers by controller radius or traverse using the actual controller when checking paths. Bridges need a walkable deck and reachable banks, not just art; canopies should not hide the approaches.

## Generation

Use known camera/style/terrain geometry. A separate concept is useful only when composition or identity remains unresolved. Show local references and attach their pixels; name fixed landmarks, path widths, light and the requested layer's content.

```text
Create the static foundation for this [camera/style] exploration scene.
Use the attached accepted master for palette, horizon and light direction.
Preserve [road, river, bridge anchor, entrances] from the layout. Terrain and
stable scenery only; leave [areas] for separate trees, doors and canopies.
Keep the travel corridor readable at [gameplay scale]. No UI, labels or actors.
```

A fixed scene without independent behavior can remain a complete picture. Do not remove distant scenery merely because it depicts buildings or trees.

## Placement

```json
{
  "props": [{
    "id": "tree-01", "image": "../props/tree/prop.png",
    "x": 420, "y": 512, "w": 160, "h": 240,
    "anchorPx": [128, 370], "sortY": 512,
    "collision": {"shape": "ellipse", "cx": 420, "cy": 510, "rx": 16, "ry": 9},
    "occlusionClass": "canopy", "occupantPolicy": {"mode": "fade-when-hidden"}
  }],
  "actors": [{"id": "player-preview", "image": "../actors/player.png", "x": 460, "y": 530}],
  "foreground": []
}
```

`anchorPx` is in original source pixels; the example assumes a 256×384 source. Collision/occupant fields are illustrative runtime metadata; the preview composer does not implement them. Prefer the actual game's schema.

Y sorting uses ground contact. Walking below leaves/roofs may require stable fade/cutaway with hysteresis, preserving the floor. Boards may use low/rear-biased dressing or `rear_shift_and_fade` for occupied cells. Do not hide every tree on any overlap. Platformers generally need stable render bands rather than changing sort order when jumping.

```bash
python scripts/compose_layered_preview.py --base map-base.png \
  --placements map-placements.json --output assembled.png \
  --resampler nearest --report assembled-report.json
```

Composer combines props, objects, actors and foreground. Foreground draws last; `sortY` controls ordering within groups. It supports per-placement resampler, scales `anchorPx` and reports clipping. Inspect at gameplay scale, then walk in front/behind, around corners, on bridges and through portals in the actual runtime. A PNG verifies composition, not gameplay, fades or video readiness.
