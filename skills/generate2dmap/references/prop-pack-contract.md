# Prop kits and extraction

Batch compact props when camera, style, scale and complete silhouettes fit their cells at sufficient detail. A 3×3 sheet gives each prop only one ninth of the image area. A collidable crate/small tree may share a kit; collision alone is not a reason to forbid batching.

Use individual/custom cells for wide roofs, large canopies, hero props and animated sources. Platforms, bridge joins and exact door openings need fixed rectangular geometry; see [side-scroll-scenes.md](side-scroll-scenes.md). Do not apply compact-prop bbox fitting to structural strips.

## Prompts and alpha

Attach the accepted style/scene master, state camera and row-major object list, and request complete silhouettes with space for tips/shadows. Layout guides can help uncertain grids but their marks must not appear in final art. Side-view props must not inherit top-down visible-top-face prompts.

For a grid, describe the cell boundaries and reserve padding inside every cell, not just around the sheet. A tree or tall post must finish above its row boundary; size it to the cell rather than giving every prop the same apparent scale. A half-empty sheet can give a tree a wider custom rectangle. Requested image dimensions and grid placement are prompt intent unless the actual provider exposes controls; always measure the returned canvas before deriving cells. One real host run returned a 1254px square after a 1536px request, with tall props crossing the midpoint.

Use verified native alpha where supported. Otherwise choose a flat chroma color absent from the art. Magenta keying destroys legitimate purple details; never key an already transparent source without a reason. An RGB checkerboard is failed transparency, not a background to remove automatically.

## Extraction

```bash
python scripts/extract_prop_pack.py --input forest-props.png \
  --rows 2 --cols 2 --labels shrub,lantern,crate,stump \
  --background-mode native_alpha --output-dir props \
  --component-mode all --min-component-area 12 --reject-edge-touch
```

- `auto` preserves existing transparency, otherwise keys magenta. Explicit `native_alpha`/`chroma_key` is clearer when known.
- Outputs are `<output-dir>/<label>/prop.png`; manifest paths are relative to the manifest directory.
- Labels must stay unique after normalization; `empty`, `skip` or `-` skips a cell. Grid dimensions must divide the source exactly.
- Trim/edge-clean default to zero. Opting in can damage tips or dark outlines; edge-touch QA checks the original keyed cell before cleanup.
- `largest` retains one component; `all` retains multipart objects. Both respect `min-component-area`; inspect intentional fragments before increasing it.
- `--reject-edge-touch` fails before image publication. It cannot repair clipping or prove semantic object separation.

Keep raw sheet, prompt and extraction manifest. Switch clipped wide objects to a suitable canvas instead of relaxing QC. Native crop recovery is acceptable only for complete separated art with explicit boxes; preserve the failed grid report.

### Recovery for a complete but off-grid pack

If visual inspection shows complete, separated silhouettes that cross the requested grid, use native source coordinates instead of resizing the sheet or cutting through objects. `--boxes-file` replaces `--rows`, `--cols`, `--labels` and `--labels-file`:

```json
{"props": [
  {"label": "tree", "source_box": [0, 0, 650, 750]},
  {"label": "lantern", "source_box": [700, 0, 1150, 750]},
  {"label": "rock", "source_box": [0, 760, 650, 1254]}
]}
```

These are measured example coordinates for a 1254x1254 source, not a universal layout. Boxes are `[left, top, right, bottom]` with exclusive right/bottom edges; they must be integer, nonoverlapping and inside the actual image. The crop, source dimensions and source hash are recorded in the manifest, and no pre-extraction rescale occurs.

```bash
python scripts/extract_prop_pack.py --input forest-props.png \
  --boxes-file measured-crops.json --background-mode native_alpha \
  --output-dir recovered-props --component-mode all --min-component-area 1 \
  --reject-edge-touch
```

Strict edge checks consider every nonzero alpha pixel before component filtering or trimming. If they fail because visually empty gutters contain tiny alpha values, inspect the histogram and light/dark composites. An explicit `--alpha-floor 4` zeros only alpha 1-4/255 in the extraction copy and records the changed-pixel count. The default 0 preserves native alpha. Never apply a large threshold to hide a clipped silhouette or erase intended translucent light/smoke; preserve the source and inspect tips after any cleanup. This changes alpha only, so native purple RGB is never keyed out. Capture a failed command's stderr as the failed-grid report; image publication remains atomic on a strict edge failure.

## Placement and motion

Static complete props may be cropped with authored support anchors. Animated frames require a registered canvas/support point; independent per-frame trimming produces bobbing. Use [$video2dsprite](../../video2dsprite/SKILL.md) for requested organic animation and poster/video alignment. Tree roots/trunk stay planted while leaf tips move; building structure stays static while smoke, cloth or lights animate.

Image rectangle, collider, interaction reach and render policy are separate. Test over light/dark backgrounds with an actor beside/in front/behind. PNG alpha correctness does not prove mobile video transparency.
