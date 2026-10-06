# HD-2D presentation: light, shadow, depth and grade over a plate

The plate stays a sharp painting; the HD-2D look comes from layers drawn over and around it. Everything here is presentation: it never moves collision, slots or interaction ranges. The data comes from the stage ([hd2d-plates.md](hd2d-plates.md)), `lights.json` and `atmosphere.json` (`generate2dmap.lights.v1` and `generate2dmap.atmosphere.v1` in [map.schema.json](schemas/map.schema.json)). The numbers below are starting points measured from shipped scenes, not rules; judge them in the game at gameplay scale, on a phone too.

Commands run from the user's project root; `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs go into the project, never into the skill folder.

## 1. Layer stack

| # | Layer | Content | Blend |
|---|---|---|---|
| 1 | Plate | the still, or the poster frame of its loop | opaque |
| 2 | Plate motion | masked environment loop (only inside its motion mask) | alpha = mask |
| 3 | Ground shadows | one soft contact ellipse per actor and standing prop | alpha (dark colour) |
| 4 | Actors and props | sorted by foot y (validate_stage `drawOrder`), tinted from the cookie, rim outline | premultiplied alpha |
| 5 | Foreground | canopies, rails and arches in front of the actors | alpha |
| 6 | Light halos | soft glow sprites at the lights' positions | additive |
| 7 | Atmosphere | mist (normal), light shafts (additive), motes (additive, or normal for leaves and petals) | as listed |
| 8 | Post | depth blur, bloom, grade, vignette, grain | full screen |
| 9 | UI | the HUD panels the layout reserved | normal |

Keep one shared clock for every animated layer and freeze it under reduced motion (motes stop, flicker and breathing hold at 1, grain drops to about half).

## 2. Light cookie: tint and flicker

    python "<skill-dir>/scripts/extract_scene_lights.py" extract --plate scene/plate.png --stage scene/stage.json --output-dir scene/lights-v1 --expect 3 --flicker 0.25:0.15

`lights.json` lists each light as `u`, `v` (UV of the plate), `color` (the hue its glow adds, at full brightness), `radius` (a fraction of the plate width, `radiusUnit` says so), `strength` (peak contrast 0..1) and an optional `flicker` `{hz, depth, phase}`. `light-cookie.png` (512x288, opaque RGB: a tint texture with no alpha channel) maps the whole plate: the `ambient` colour everywhere, each light a smooth pool of its colour.

The detector proposes candidates; it does not know what a lamp is. It finds peaks of local contrast and measures each against its own peak, so a lantern on a lit wall stays separate from the wall, and broad bright areas (a sunlit wall, a sky gap) are dropped. Peaks are examined strongest first until `--max-lights` lights are found: on detailed night paintings the strongest are mostly lamps, flames and lit windows, but rows of windows, wet-floor glints and water reflections also rank, and a lamp no brighter than its surroundings is missed. Give `--stage`: the plate contract keeps lamps off the walkable ground, so candidates centred on ground polygons or ripple water are rejected as reflections (`--keep-floor` keeps them). Use `--max-lights` near the number of lights you expect plus a few. Then curate: look at `lights-overlay.png`, delete false lights from `lights.json`, add missed ones (`u`, `v`, `color`, `radius`), and re-bake the cookie:

    python "<skill-dir>/scripts/extract_scene_lights.py" cookie --lights scene/lights-v1/lights.json --plate scene/plate.png --output-dir scene/lights-v2

Tint each actor once per frame by sampling the cookie at its foot (flip v for GL textures):

```glsl
float light = max(max(cookie.r, cookie.g), cookie.b);          // texture2D(uCookie, footUv)
light *= 1.0 - flicker.depth * (0.5 + 0.5 * sin(6.2832 * (flicker.hz * time + flicker.phase)));
vec3 tint = mix(vec3(0.50, 0.58, 0.76), vec3(1.32, 1.08, 0.84), clamp(light, 0.0, 1.0));
gl_FragColor = vec4(sprite.rgb * tint, sprite.a);               // cool in the dark, warm near a lamp
```

Keep flicker slow and shallow: 0.2-0.3 Hz breathing with depth 0.1-0.2 reads as living flame; anything above 3 Hz with a large depth risks photosensitive viewers (the QA warns). Halo sprites at each light pulse with the same function, scaled by `strength`.

## 3. Ground shadows

A shadow is a radial gradient squashed to the footprint ratio (0.58, the y-squash of the stage footprint): dark centre about rgba(25, 40, 31, 0.3) fading to transparent, width about 0.7 of the actor's body width, centred on the foot root, drawn before every actor so a nearer actor covers a farther one's shadow. Shift it a few pixels away from the strongest nearby light and shrink it under jumps. Painted shadows in the plate stay; do not draw a second shadow over a painted one.

## 4. Depth of field (tilt-shift)

Keep the playable band sharp and blur what lies above it (far) and below it (near) by mixing toward a half-resolution blurred copy of the frame:

```glsl
float far  = smoothstep(0.0, 0.15, band.x - uv.y);  // rows above the playable band
float near = smoothstep(0.0, 0.10, uv.y - band.y);  // rows below it
float blur = clamp(far * 0.60 + near * 0.42, 0.0, 1.0);
color = mix(color, blurred, blur);
```

With a 3D ground mesh use the depth buffer instead (focus at the actors' depth). Never blur the actors themselves; HUD text stays sharp.

## 5. Bloom and grade

Bloom: downsample to half size, keep the bright part (threshold 0.82 on the largest channel with a soft knee of 0.45), blur it at 1/4 and 1/8 size, and add both: `color += bloom * grade.bloom + wide * (0.7 * grade.bloom)`. Shipped scenes use `grade.bloom` 0.42-0.58.

Grade, in display space after tone mapping:

```glsl
float L = dot(c, vec3(0.2126, 0.7152, 0.0722));
c = mix(vec3(L), c, grade.saturation);                                    // 1.05-1.10
c += shadowTint * (1.0 - smoothstep(0.0, 0.35, L)) * (0.4 + 0.6 * smoothstep(0.0, 0.12, L))
   + highlightTint * smoothstep(0.5, 1.0, L);                            // split tone
c *= 1.0 - smoothstep(0.34, 0.98, length((uv - 0.5) * vec2(1.0, 0.82))) * grade.vignette;  // 0.20-0.34
c += (hash(pixel, time) - 0.5) * 0.022;                                   // grain, 0.012 with reduced motion
```

`grade.splitTone` stores colours: one mapping is `shadowTint = shadows / 255 * amount * 0.1` and `highlightTint = highlights / 255 * amount * 0.1`, which gives the 0.01-0.03 offsets shipped scenes use.

## 6. Atmosphere

```json
{
  "schema": "generate2dmap.atmosphere.v1",
  "motes": [{"count": 120, "mobile": 40, "size": 0.2, "speed": 0.8, "sway": 1, "opacity": 0.85, "colors": ["#ffd27a", "#ffe7a8", "#ffb45c"], "blend": "additive"}],
  "mist": {"color": "#6fa9b2", "opacity": 0.1, "scale": 0.1},
  "shafts": [{"color": "#c4e2ff", "opacity": 0.1, "spots": [[-9, -4], [5, -6]]}],
  "grade": {"bloom": 0.5, "saturation": 1.07, "splitTone": {"shadows": "#0a1830", "highlights": "#ffe0b0", "amount": 0.3}, "vignette": 0.28}
}
```

Motes are GPU points whose motion is a pure function of the shared clock (wander and blink, rise, fall and flutter, drift); `mobile` is the count used on phones. Mist is a slow noise layer over the ground (opacity 0.07-0.11); shafts are additive streaks (opacity 0.07-0.11), off on phones. Check the values and the mote budget:

    python "<skill-dir>/scripts/extract_scene_lights.py" atmosphere --atmosphere scene/atmosphere.json --output-dir scene/atmosphere-qa-v1 --max-motes 512 --max-mobile-motes 160

It fails on invalid values (opacity or vignette outside 0..1, bad colours, `mobile` above `count`) and warns on heavy mist (over 0.3) or shafts (over 0.25), bloom over 1, saturation outside 0.7..1.4, vignette over 0.6 and mote counts over the budgets.

## 7. Orthographic ground mesh

When the game needs real depth (3D lights, shadows on the ground, a camera that moves a little), rebuild the painted ground as a plane:

- Triangulate each ground polygon (`groundPolygons * sourceSize`, plate pixels) and place its vertices on the world plane: `X = px - W/2`, `Y = 0`, `Z = (py - H/2) / sin(theta)`, where theta is the painted camera angle above the ground (about 20 degrees).
- Give each vertex the plate UV `(px / W, 1 - py / H)`, so the mesh shows exactly the painted pixels.
- Use an orthographic camera pitched theta below horizontal with a frustum of W x H plate pixels; the mesh then lands on the painting pixel for pixel.
- Actors are camera-facing billboards standing at their foot's world point with height `visible_px / cos(theta)`; depth sorting and ground shadows come from the 3D order.

The 2D route (plate quad plus foot-y sorted sprites) is enough for battles and fixed rooms; the mesh is worth it when lights or shadows must sit on the ground.

## 8. Rim outline

A thin screen-space outline keeps actors readable on a busy plate. Sample the sprite alpha at 8 neighbours `outlinePixels` away (1.35 x devicePixelRatio; update the uniform on every resize), then composite a light rim and a darker ink ring under the sprite:

```glsl
float rim = smoothstep(0.08, 0.65, maxNeighbourAlpha(1.0 * offset)) * 0.88;
float ink = smoothstep(0.08, 0.65, maxNeighbourAlpha(1.65 * offset)) * 0.40;
float a = c.a + (rim + ink * (1.0 - rim)) * (1.0 - c.a);
vec3 rgb = c.rgb * c.a + (vec3(0.98, 0.94, 0.80) * rim + vec3(0.14, 0.22, 0.20) * ink * (1.0 - rim)) * (1.0 - c.a);
c = vec4(rgb / max(a, 1e-4), a);
```

Blend pose transitions in premultiplied space before the outline, so a cross-fade never shows the floor through the body. Keep the outline on heroes at least; for pixel art use a 1 logical-pixel outline instead.

## 9. Limits

- Light positions, colours and radii are measured from the painting's glows, not from real emitters, and the candidate list needs a human or agent review on detailed art; the cookie is a screen-space tint, not light transport.
- The atmosphere check validates ranges and counts; frame time on a target phone must be measured.
- Shader snippets are recipes, not a tested runtime: no renderer ships with this skill.
