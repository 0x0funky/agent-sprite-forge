---
name: generate2dspriteapi
description: "Generate 2D game sprites and animation sheets through an OpenAI-compatible Image API, then reuse generate2dsprite's local cleanup, frame extraction, alignment, QC, and export pipeline. Use when API-backed raw image generation is required instead of the host agent's built-in image generation."
---

# Generate2dspriteAPI

Use this skill when raw sprite art must be generated through an OpenAI-compatible Image API rather than the host agent's built-in image generation.

## Shared workflow dependency

This is a thin API backend adapter for `$generate2dsprite`, not a second copy of the sprite pipeline.

Before doing asset work, read the sibling skill installed next to this folder:

- `../generate2dsprite/SKILL.md`
- `../generate2dsprite/references/modes.md` when asset planning is ambiguous
- `../generate2dsprite/references/prompt-rules.md` when writing the image prompt

Use the sibling skill's asset planning, sheet-selection, animation containment, anchor, scale-profile, QC, and return-bundle rules as the normative workflow. Resolve these paths relative to this skill's directory, not relative to the user's project.

The shared local tools are also owned by `$generate2dsprite`:

- `../generate2dsprite/scripts/generate2dsprite.py`
- `../generate2dsprite/scripts/make_anchor_layout.py`
- `../generate2dsprite/scripts/make_layout_guide.py`

Do not copy those scripts or the shared references into this skill. If the sibling skill is not installed, stop and report that `$generate2dspriteapi` requires `$generate2dsprite` beside it.

## Backend-specific rules

- Use this skill's `scripts/genimg.py` for every raw image. Do not call built-in `image_gen` for this skill.
- Write the creative prompt yourself using the sibling prompt rules. Include the exact grid, solid `#FF00FF` background, stable identity, camera distance, safe area, action constraints, and no-cell-edge-crossing requirements.
- `genimg.py` is text-prompt-only. For a local reference, inspect it with `view_image` and translate its visual constraints into the prompt. Do not pass an unseen file path as if it were an image input.
- If exact image-to-image editing is required, stop and report that this backend needs image-input support before proceeding.
- Save each raw API result as an explicit PNG in the run directory and save the exact prompt as `prompt-used.txt`.
- If the API call fails, stop before postprocessing. Do not silently fall back to built-in image generation.

## API generation

Read [references/genimg-api.md](references/genimg-api.md) for user-level configuration and command examples. The wrapper supports:

- `OPENAI_API_KEY`
- optional `OPENAI_BASE_URL`
- user-level `~/.codex/generate2dspriteapi.env`
- per-invocation `--base-url` / `--api-url`

The command-line URL overrides the process environment, which overrides the user-level configuration file. Keep API credentials outside repositories.

## Postprocess and QC

After generating a raw image, follow the shared `$generate2dsprite` workflow exactly:

1. Use `../generate2dsprite/scripts/generate2dsprite.py process` for cleanup, frame extraction, alignment, scaling, QC, transparent PNGs, and GIF output.
2. Use the shared anchor/layout scripts only when the sibling workflow calls for them.
3. Review the processed sheet and QC metadata. Regenerate the raw image when the subject is clipped, drifts in scale, touches output edges, or fails an action-specific gate.
4. Return the same bundle artifacts expected by `$generate2dsprite`.

API generation changes only the raw-image backend; it does not change the sprite pipeline's asset decisions or QC standards.
