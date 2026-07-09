# Video2dsprite prompt rules

## Base still (`image_gen` / `image_edit`)

Required:

- Solid flat background `#FF00FF` (pure magenta), no gradient, no floor shadow if possible
- Full body, centered, generous margin on all sides
- Side or 3/4-side view for run/walk
- Same scale and costume the game already uses when a reference exists
- No text, UI, watermark, speech bubbles, second character

Good base pattern:

```text
Side-view full-body 2D game sprite of <subject>, <style>, standing ready pose,
facing right, centered in frame, feet near lower third, solid flat magenta
background #FF00FF only, no ground, no shadow, no text, crisp readable silhouette.
```

If matching a project sprite: use `image_edit` with the existing frame as reference and only change pose/background to magenta if needed. Prefer compositing a known good frame onto magenta in code when the art already exists.

## Video (`image_to_video`)

Write **one short present-tense shot** (1–2 sentences). Constraints:

| Do | Don't |
| --- | --- |
| Run/walk **in place** (treadmill) | Travel across the screen |
| Locked camera | Pan, zoom, orbit, handheld |
| Keep solid magenta background | Scenic BG, ground scroll, particles filling frame |
| Single continuous action | Combo attacks + movement + camera |
| Stable identity/clothes | Costume change mid-clip |

Run example:

```text
The same chibi ninja character runs in place facing right with a classic side-scroller
stride, arms trailing slightly back, body centered, camera locked, solid flat magenta
background only.
```

Walk / idle examples:

```text
The character walks in place facing right with a steady side-view walk cycle, camera locked, solid magenta background.
```

```text
The character idles in place with a subtle breathing bob and weight shift, camera locked, solid magenta background.
```

Attack (use carefully — identity drift is higher):

```text
The character performs a single short punch combo in place facing right, camera locked, solid magenta background, no screen-filling FX.
```

## Sampling guidance (post-video)

- Export multiple densities: 8 / 16 / 24 / 48 for comparison GIFs
- For engine integration: pick one smooth cycle (~12–16 frames) after watching previews
- If the 6s clip contains multiple run cycles, denser even sampling across the whole clip can look like a long multi-cycle animation — that is OK for previews, but for a game loop re-sample one cycle region manually if needed
