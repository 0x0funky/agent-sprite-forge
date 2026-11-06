# Third-party notices

Agent Sprite Forge is MIT licensed (see [LICENSE](./LICENSE)) and bundles no third-party code. One optional
route runs an external project that you install yourself.

## pixelfx

- **Project:** [pixelfx](https://github.com/patch-gg/pixelfx), code-driven pixel-art effects and animations.
- **Author:** Patch.
- **License:** MIT. Copyright (c) 2026 Patch; Copyright (c) 2026 Hugo Duprez.
- **Used by:** `skills/generate2dsprite/scripts/fx_pixelfx.py`, which lists and renders effects for hibit games.
  It runs your own checkout (`--pixelfx <dir>` or `PIXELFX_DIR`, from `git clone https://github.com/patch-gg/pixelfx`)
  with Node.js. No pixelfx code is copied into this repository.
- **pixelfx's own notice** credits:
  - Sprite Fusion's open-source pixel-art VFX (https://www.spritefusion.com/pixel-art-vfx), Copyright (c) 2026
    Hugo Duprez, MIT License: adapted in pixelfx `src/core/sim.js` and the effects that say so.
  - Hollow Crown (https://github.com/rehan-remade/hollow-crown), Copyright (c) 2026 rehan-remade, MIT License:
    adapted in pixelfx `src/core/act.js`.
  - The Patch Pixel font in pixelfx `gallery/fonts` is Patch's own and is **not** covered by pixelfx's MIT
    license. The Forge never uses it.
- **What ships with a render:** every `fx_pixelfx.py render` writes `CREDITS.txt` (pixelfx's full LICENSE and
  NOTICE.md) and `fx.json` (repository, commit, version, license, copyright lines, effect and options) beside the
  frames. Keep `CREDITS.txt` with the frames you ship and credit pixelfx in your game's credits.
- **Disclosure:** rendered effects are code-drawn art by pixelfx, not image-model output; say so when you show
  or ship them.
