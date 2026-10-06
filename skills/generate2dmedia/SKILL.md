---
name: generate2dmedia
description: The art-route layer of the Forge skills. route_media.py is the one command that generates an image or an image-to-video clip - through the paid OpenAI or xAI API when a key is configured (the configured key is the owner's consent), else through the user's own signed-in Codex or Grok CLI - and prints one JSON line with the route, artifact, sha256 and cost estimate; with no route it answers no-route (codeart2d is then the last resort). Also the capability check (forge_doctor), API key configuration, the spend ledger, resume and Codex image adoption. Use when a Forge skill sends a generation here, for the capability check, or when the user asks about keys, routes or spend. Not for planning, processing or QA of sprites, video frames or maps (generate2dsprite, video2dsprite, generate2dmap) or for code-drawn art (codeart2d).
---

# Generate 2D Media

The route layer. It makes raw media; the owning skill plans the asset before and checks it after.

## Use when

- Any Forge skill (or its pipeline script) needs a generated image, reference edit or image-to-video clip.
- Once per session, to see which routes work here (`forge_doctor.py`).
- The user asks to configure an API key, to use a particular route, or about spend.

## Do not use when

- Planning, keying, registering or packaging: [generate2dsprite](../generate2dsprite/SKILL.md), [video2dsprite](../video2dsprite/SKILL.md), [generate2dmap](../generate2dmap/SKILL.md).
- Code-drawn art the user asked for: [codeart2d](../codeart2d/SKILL.md).

## Route order (owner decision 2026-10-06)

1. **API**, when a key is configured: images OpenAI (`gpt-image-2.5-sunburst`), then xAI (`grok-imagine-image-2.0`); video xAI (`grok-imagine-video-1.5`). A configured key is the owner's consent: no per-call question.
2. **Local**: your own image tool if you have one (Codex `image_gen`, Grok's native tools), otherwise the user's signed-in CLI: Codex (local CLI) `image_gen` with references attached, then Grok (local CLI) one-shot image or edit; video Grok (local CLI) in ACP mode. An installed CLI is used as is; its first successful run records the VERIFIED proof.
3. **codeart2d**, only when the user explicitly asks for code-drawn art or `route_media.py` prints `no-route` (exit 3).

Always name the route that ran ("OpenAI API", "xAI API", "Codex (local CLI)", "Grok (local CLI)"). Details: [route-media.md](references/route-media.md).

## Capability check (once per session)

Run `python "<skill-dir>/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Its `ROUTES` block shows which API keys are configured (yes or no, never a key), the local readiness of each CLI and the resolved order (`routeOrder`). If `encoding.stdout` fails, set `PYTHONUTF8=1`.

## Generate (`route_media.py`)

| Need | Command |
|---|---|
| Which route would run | `python "<skill-dir>/scripts/route_media.py" resolve --kind image` (or `video`; `--references N`) |
| One image, still or edit | `python "<skill-dir>/scripts/route_media.py" image --prompt-file <txt> --reference <png> --size 1024x1024 --out-dir <new>` (`--reference` repeatable, in the order the prompt names them) |
| Animate an approved still | `python "<skill-dir>/scripts/route_media.py" video --prompt-file <txt> --reference <still.png> --last-frame <still.png> --duration 6 --resolution 720p --out-dir <new>` |
| A route of the user's choice | add `--route api`, `local`, `openai`, `xai`, `codex-cli`, `grok-cli` or `grok-acp` |
| The plan and estimate first | add `--dry-run` (no key is used, nothing is sent or written) |

Success prints one ASCII JSON line and exits 0: `{"status":"ok","route":"api:openai","artifact":"<out-dir>/generated.png","sha256":...,"estimateUsd":...}` plus `model`, `job` and, for video, `lastFrameUsed`. No route prints `{"status":"no-route","fallback":"codeart2d"}` and exits 3. A failure prints one `error: <route>: <code>: <message>` line and exits 1. A route whose account cannot serve the request (no credit, no access, not signed in) passes it to the next route; a refusal such as moderation stops. Write prompts yourself into UTF-8 files; local routes take the size and every instruction from the prompt.

## Keys

Keys come from `OPENAI_API_KEY` / `XAI_API_KEY` in the environment, or from the user config file, never from a project: `%APPDATA%\agent-sprite-forge\config.json` on Windows, `~/.config/agent-sprite-forge/config.json` elsewhere (`$XDG_CONFIG_HOME` when set), as `{"OPENAI_API_KEY": "...", "XAI_API_KEY": "..."}` with optional `"models"`. Never print, log, commit or paste a key, and never put one in a prompt. A Codex or Grok sign-in is never an API key.

## Spend and the ledger

Every call, paid or local, is a line in `<project>/.forge/ledger.jsonl` with its estimate (`estimateUsd` is null when no verified price exists; say "price unknown"). There is no cap by default; `--budget-usd`, `--max-calls`, `FORGE_MAX_PAID_REQUESTS` and `FORGE_SESSION_IMAGES` / `FORGE_SESSION_VIDEOS` set one when the user asks. `python "<skill-dir>/scripts/media_ledger.py" summary` shows spend; `media_ledger.py settle <id> --status ...` closes an unknown outcome.

## Lower-level tools

| Need | Command |
|---|---|
| One API request by hand (dry run until `--execute`) | `generate_media.py image` or `video` with `--provider`, `--model`, `--prompt-file`, `--out-dir` ([api-usage.md](references/api-usage.md)) |
| Poll or download an xAI video job without paying again | `generate_media.py resume --job <out>/job.json` |
| One local CLI run by hand | `cli_media.py image --route codex-cli --prompt-file <txt> --output-dir <new> --execute` ([cli-routes.md](references/cli-routes.md)) |
| A local run timed out or was interrupted | `cli_media.py resume --run <id>`, then `--adopt` (never reruns the CLI) |
| Codex made an image that is not in the project | `cli_media.py adopt --codex-thread <thread-id> --output-dir <new>` |
| Many jobs | `generate_media.py batch` or `cli_media.py batch` |
| Verify a local route ahead of time (optional) | `forge_doctor.py --verify-route <route> --execute` (one quota call) |

## Host notes

- **Codex:** `image_gen` is your own tool: with no configured key use it and adopt the result (`cli_media.py adopt`); scripts reach Codex (local CLI) through `route_media.py`. This skill is explicit-only in Codex.
- **Claude Code:** no media tools; `route_media.py` takes the API or the local CLIs. `<skill-dir>` is `${CLAUDE_SKILL_DIR}`; look at results with Read.
- **Grok:** its native media tools are your own tools; the same `grok` executable is the "Grok (local CLI)" route for other hosts. Agent setups: [agent-profiles/README.md](references/agent-profiles/README.md).

## Commands and outputs

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. Outputs go to a new `--out-dir` inside the project. Results are raw media: hand them to the owning skill for identity, size, alpha, motion and seam checks. Keep prompts, input hashes, job ids and result hashes; report the model requested and the model returned separately. Providers: [provider-survey.md](references/provider-survey.md); prices: `references/prices.json`; data contracts: `references/schemas/media.schema.json`.
