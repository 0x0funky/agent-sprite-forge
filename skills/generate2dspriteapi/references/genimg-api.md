# genimg.py API workflow

This skill uses its bundled wrapper at:

```text
skills/generate2dspriteapi/scripts/genimg.py
```

The wrapper sends a text prompt to the configured OpenAI-compatible Image API and saves the returned Base64 image locally. It does not accept image reference inputs or perform image editing. It can load a user-level configuration file so the skill works from any project.

## Setup

Install the API client in the Python environment used by the skill:

```bash
python3 -m pip install openai
```

For a machine-wide Codex setup, create this user-level file:

```text
~/.codex/generate2dspriteapi.env
```

Use simple `KEY=value` lines. Keep the file outside the repository and restrict it to the current user:

```bash
mkdir -p ~/.codex
chmod 700 ~/.codex
chmod 600 ~/.codex/generate2dspriteapi.env
```

For the official OpenAI API, configure only the key and leave the base URL unset:

```text
OPENAI_API_KEY=your-api-key
```

For an OpenAI-compatible gateway, configure the matching key and base URL together:

```text
OPENAI_API_KEY=provider-api-key
OPENAI_BASE_URL=https://example.com/v1
```

The wrapper loads this file without overriding environment variables that are already set. This gives the following precedence:

```text
command-line --base-url / --api-url
    > current process environment
    > ~/.codex/generate2dspriteapi.env
```

An OpenAI-compatible API base URL can also be selected with either the environment variable or a command-line flag:

```bash
export OPENAI_BASE_URL="https://example.com/v1"
# or use --base-url / --api-url for one invocation
```

The command-line URL overrides `OPENAI_BASE_URL`. Pass the base URL, not the complete image endpoint: `https://example.com/v1`, not `https://example.com/v1/images/generations`.

## Raw-sheet command

Use an explicit output path inside the current run directory:

```bash
python3 skills/generate2dspriteapi/scripts/genimg.py \
  "<agent-written sprite prompt>" \
  --output <run-dir>/raw-sheet.png \
  --model gpt-image-2 \
  --size 1024x1024 \
  --quality high
```

Choose `--size` according to the requested sheet shape and available API support. Keep the prompt in `<run-dir>/prompt-used.txt`. Do not add a full `/images/generations` path to `--base-url`.

## Prompt and reference behavior

The command accepts one text prompt only. If the API Key is invalid or the provider does not support the requested model, the command stops before postprocessing; do not silently fall back to built-in image generation. If the user gives a local reference, use `view_image` to inspect it and write its important visual constraints into the prompt. Do not put only a file path in the prompt and assume the API can see it. If the request requires exact image-to-image editing, this skill is not sufficient without extending `genimg.py` to send image inputs.

The generated raw image must still satisfy the normal sprite rules: solid `#FF00FF` background, exact grid, no visible guide boxes or labels, consistent subject identity, and no cell-edge clipping. After generation, use the copied `scripts/generate2dsprite.py` only for deterministic cleanup, frame extraction, alignment, QC, transparent export, and GIF creation.
