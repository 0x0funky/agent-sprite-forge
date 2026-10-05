# Generation prompts

Compose a brief for the chosen tool's actual image/reference contract. Host image tools and the explicitly selected API adapter are both valid; the provider's schema decides supported size, alpha and reference options. Save requested settings and inspect the returned pixels separately.

## Prompt structure

1. Asset and game camera: side elevation, top-down or three-quarter, facing and intended game size.
2. Accepted identity: silhouette, palette, costume, anatomy, held equipment and lighting. Name each reference's role.
3. Motion phases or static prop purpose, with explicit allowed motion and fixed structural features.
4. Registration: one canvas, standing-equivalent scale, root and safe motion envelope. The whole action must fit, not only its neutral pose.
5. Background: real transparency if supported; otherwise one uniform chroma color absent from the subject. No text, labels, grid lines or painted checkerboard.

Example grounded attack brief:

```text
Reference 1 is the accepted side-view hero and fixes identity, palette, anatomy,
clothing and camera distance. Create six phases in a 2-row, 3-column equal-cell
grid, read left to right: anticipation, wind-up, strike, follow-through,
recovery, ready. Keep a single standing-equivalent body scale and shared root.
Allow natural torso compression; do not enlarge each pose to fill its cell.
Reserve the full weapon/body envelope with padding in every cell. Body and
held weapon only; no detached slash arc, impact, projectile, dust or ground plate.
Background: <actual alpha or chroma contract>. No visible grid marks or text.
```

Use the project's style. Do not force pixel art for painted map props, front-facing perspective for a side scroller, or a particular franchise's design. Strict pixel art needs deliberate clusters and palette at game size; HD-2D can combine crisp actors with painted/3D scenery and separate light/particle layers.

## Cell containment and recovery

Prompt for the **full motion envelope** inside each cell, not a large body that fills 80%+ in every pose. A useful first-sheet margin is 15% clear background on every side, with one anatomical scale across the action. Count the longest tail, widest stride, ears and held equipment in that envelope. State that no part may touch or cross an internal cell boundary, and that gutters/separators must not be drawn. These margins are a generation request, not evidence that extraction will be safe.

Inspect the returned sheet and every exported frame. A contour crossing a nominal boundary can create both a cut limb/tail and an unrelated fragment in its neighbor, even when dimensions divide exactly and packaging passes. Do not enable a largest-component filter merely to hide this evidence: it can remove intended pieces.

If complete subjects remain separated in the raw source, use visually verified, equal-size `assemble_frames.py --crop-boxes boxes.json` rectangles to isolate them. Keep the raw source and record every crop origin/change, then re-check padding and all extracted frames. Different crop origins translate content relative to the runtime root; preserve registration with common padding, or make and record a reviewed integer registration repair. Do not silently treat the new crop bottom as the root. If complete contours cannot be recovered without overlap, clipping or guessing missing pixels, regenerate the affected source with more envelope margin. None of these steps require changing the crop algorithm.

## Motion decisions that prevent recurring defects

- **Grounded idle:** restrained torso compression and secondary hair/clothing motion. For a massive boss, fixed feet and pelvis with chest/core pulses and shoulder settling; avoid sliding the entire body side to side.
- **Run/walk:** distinct contact, passing and compression phases. Preserve planned vertical body motion and flight where appropriate. See [character-animation.md](character-animation.md); pinning every opaque bottom produces skating or dead motion.
- **Attack/cast/shoot:** one body action per raw sheet/clip. Keep large slash, muzzle flash, projectile, aura and hit FX separate. An integrated effect is acceptable only if the runtime preserves a larger shared canvas/root without shrinking the body.
- **Long quadruped or serpent:** keep the torso/root registered and all poses inside one shared silhouette envelope; tuck tails as necessary. Express in-place bites/pounces through local articulation. Actual travel belongs to runtime motion unless root motion is explicitly authored.
- **Jump/knockback/flying:** shared source root and motion-relative phases; not a fixed feet line.
- **Ground-contact fire:** one fixed ignition baseline, moving flame tips, no baked floor plate or shadow. Detached embers cannot define the ground contact.
- **Projectile/impact:** preserve travel direction and origin; include space for expansion/fade. Do not normalize each phase to the same silhouette size.

## Sheet, full frames, or video

Choose per-frame detail and useful phases before the sheet size. A 2x2 may suit a restrained four-pose loop; 2x3 or 3x3 can add useful phases. More cells reduce available source pixels per pose. A 4x4 directional walk is one locomotion family; a hero's unrelated idle/run/attack/death should be generated separately and packed later.

Individual full frames retain detail but cost more calls and need stronger identity/registration checks. Keep the same master and camera across every phase; never silently resize individual canvases. Use `assemble_frames.py` for fixed-cell extraction/full-frame packaging and `build_animation_clips.py` for action timing.

For fluid secondary motion, prefer an accepted master followed by a short image-to-video sample before multiplying actions. See [video-handoff.md](video-handoff.md). Video does not guarantee stable pixels, transparent background, a loop seam or foot contact.

## Optional guides

`make_layout_guide.py` draws cell geometry; `make_anchor_layout.py` repeats an accepted master at the requested scale and root. Use these when geometry drifts, not as a mandatory extra generation step. Inspect and attach them as actual reference images. State: "Use this only for slots, scale and registration; do not reproduce boxes, marks, labels or separators."

A grounded anchor template constrains a reference root, not every pose's visible feet. For run flight, recoil, jump or a large changing silhouette, use a suitable motion envelope. A processing profile cannot fix inconsistent model-generated anatomy.

For map props, square packs suit compact rocks/crates/shrubs. Bridges, platforms, walls, doors, houses, large trees and other structural pieces need explicit native dimensions, joins and collision geometry. Route these to the map skill rather than fitting them into arbitrary square cells.
