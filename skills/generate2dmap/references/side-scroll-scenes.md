# Side-view structure

Plan controller size, jump envelope, one-way platforms, slopes and hazards first. A brawler uses a walkable belt/depth lanes. Stage length follows scope. Scenery is noncolliding depth; independently controlled platforms/doors/hazards are runtime objects or tiles. Artwork skins explicit geometry, not inferred texture.

Parallax is optional for a simple scene; a layered-scrolling request needs independent depth artwork. Read [parallax-backgrounds.md](parallax-backgrounds.md).

## Shared structural strips

Generate left cap, middle repeat and right cap at one camera/lighting scale. Preserve native rectangular crops and measured walkable top. Outer cap padding is allowed, join edges must meet. Never independently bbox-trim/resize pieces.

Example for a 192×64 atlas:

```json
{
  "surface_y_px": 16, "collision_depth_px": 24,
  "pieces": [
    {"id": "left", "role": "left_cap", "source_box": [0,0,64,64], "collision_span_px": [8,64]},
    {"id": "middle", "role": "middle", "source_box": [64,0,128,64]},
    {"id": "right", "role": "right_cap", "source_box": [128,0,192,64], "collision_span_px": [0,56]}
  ]
}
```

```bash
python scripts/extract_platform_strip.py --input platform.png --spec platform.json \
  --output-dir platform-kit-new --background-mode native_alpha --strict-qc
```

Modes are `native_alpha`, `chroma_key`, `opaque`. No trim, resizing or inferred anchor. Source/spec hashes and crops appear in `platform-strip.json`; use a new output directory. The helper emits a left + three middles + right preview after QC.

All declared collision-span columns must cover the top and meet the alpha-band threshold. Default is fully opaque/full coverage; reviewed tolerance may suit translucent decorative surfaces but collision stays authored. Optional seam RGB/alpha thresholds are separate diagnostics, not artistic seamlessness proof.

At uniform scale `s`:

```text
imageLeft = collisionLeft - s * collision_span_px[0]
imageTop  = collisionTop  - s * surface_y_px
```

Cap padding means visual-left can differ from collision-left. Reuse the same kit across stage segments, repeat the middle at its declared width, and test actual controller crossing of every join. Pixel snapping may be needed even with aligned source geometry. Verify camera/portal coverage, one-way/slopes and actor bands while airborne. A stage mockup is optional when a proven layout and kit exist.
