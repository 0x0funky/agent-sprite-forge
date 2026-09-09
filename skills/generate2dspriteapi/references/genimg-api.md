# genimg.py API workflow

`$generate2dspriteapi` bundles its only API-specific script here:

```text
skills/generate2dspriteapi/scripts/genimg.py
```

All sprite planning and local postprocessing remain in the sibling `$generate2dsprite` skill:

```text
skills/generate2dsprite/
```

The wrapper sends a text prompt to the configured OpenAI Images API or an OpenAI-compatible provider and saves the returned Base64 image locally. It does not accept image reference inputs or perform image editing.

## User-level setup

Install the API client in the Python environment used by the skill:

```bash
python3 -m pip install openai
```

For a machine-wide Codex setup, create this user-level file:

```text
~/.codex/generate2dspriteapi.env
```

The file uses simple `KEY=value` lines and is read without executing shell code. Keep it outside repositories and restrict it to the current user:

```bash
mkdir -p ~/.codex
chmod 700 ~/.codex
chmod 600 ~/.codex/generate2dspriteapi.env
```

For the official OpenAI API, configure only the key and leave the base URL unset:

```text
OPENAI_API_KEY=your-api-key
```

For an OpenAI-compatible provider, configure the matching key and base URL together:

```text
OPENAI_API_KEY=provider-api-key
OPENAI_BASE_URL=https://example.com/v1
```

The wrapper does not override environment variables already present in the process. The effective precedence is:

```text
--base-url / --api-url
    > current process environment
    > ~/.codex/generate2dspriteapi.env
```

`OPENAI_BASE_URL` must be a base URL, not a complete image endpoint:

```text
Correct: https://example.com/v1
Wrong:   https://example.com/v1/images/generations
```

## Raw image command

Resolve `<api-skill-dir>` to the directory containing this skill's `SKILL.md`, then run:

```bash
python3 <api-skill-dir>/scripts/genimg.py \
  "<agent-written sprite prompt>" \
  --output <run-dir>/raw-sheet.png \
  --model gpt-image-2 \
  --size 1024x1024 \
  --quality high
```

Keep the exact prompt in:

```text
<run-dir>/prompt-used.txt
```

After this command succeeds, pass the raw PNG to the sibling processor, for example:

```bash
python3 <api-skill-dir>/../generate2dsprite/scripts/generate2dsprite.py process \
  --input <run-dir>/raw-sheet.png \
  --target creature \
  --mode idle \
  --rows 3 \
  --cols 3 \
  --output-dir <run-dir>/processed \
  --strict-qc
```

The exact processor flags remain asset-specific and must follow `$generate2dsprite`'s rules. If the API call fails, do not fall back to built-in image generation or run postprocessing on a missing/partial result.
