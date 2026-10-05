---
name: generate2dmedia
description: Media routes, consent, budget and ledger for the Forge skills. Run forge_doctor once per session to see which art routes work; generate images, reference edits or image-to-video through a VERIFIED local agent (Codex or Grok local CLI, cli_media --route auto) or, with the user's consent for each request, the paid OpenAI or xAI REST APIs; keep every call in a ledger under session caps; resume interrupted jobs and adopt Codex images. Use only when a Forge skill routes here, for the capability check, or when the user explicitly asks for a local-CLI or API generation. Not for planning, processing or QA of sprites, video frames or maps (generate2dsprite, video2dsprite, generate2dmap) or for code-drawn art (codeart2d).
---

# Generate 2D Media

The route, consent and budget layer. It makes raw media; the owning skill plans the asset before
and checks it after. Installing this skill never authorizes bypassing a host's tool policy or a paid call.

## Use when

- Once per session, to learn which art routes work here (`forge_doctor.py`).
- A Forge skill picked a local-agent or paid route for an image, an edit or an image-to-video job.
- The user asks to spend their Codex or Grok quota, or approves a paid API call.

## Do not use when

- Planning, keying, registering or packaging: [generate2dsprite](../generate2dsprite/SKILL.md), [video2dsprite](../video2dsprite/SKILL.md), [generate2dmap](../generate2dmap/SKILL.md).
- The request fits the code-art envelope (small pixel sprites, FX, tiles, map data): [codeart2d](../codeart2d/SKILL.md).
- The host's own media tool can do the job: use it directly.

## Capability check (once per session)

Run `python "<skill-dir>/scripts/forge_doctor.py" --host-tools <tools> --save <output>/doctor.json`, where `<tools>` lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`. Use only the routes its `ROUTES` block names; they come local agent first. `ready` means use it now: your own tool, or Codex / Grok (local CLI) VERIFIED for the installed version, which runs without a per-call question within the session cap (8 images and 2 videos per 12 hours). `consent` (the paid API) needs the user's consent for each request. An installed CLI is not a connected tool: a CLI route is offered only after a verified run for that CLI version. If `encoding.stdout` fails, set `PYTHONUTF8=1`.

## Route order (local agent first)

Code art keeps its envelope on every host: small pixel sprites (up to 48 px visible height, 49-64 px with consent), FX, tiles and map data go to codeart2d first.

- **Images and edits:** (1) the host's own image tool (Codex `image_gen`); (2) Codex (local CLI) `image_gen`; (3) Grok (local CLI) one-shot `image_gen` or `image_edit`; routes 2 and 3 only when VERIFIED; (4) the paid REST API with explicit consent for that request; (5) otherwise explain the gap.
- **Video:** (1) Grok (local CLI) in ACP mode (`image_to_video`) when VERIFIED; (2) REST with consent; (3) a video the user has; (4) otherwise explain the gap.

Tell the user about one "Grok (local CLI)" route (one-shot for images, ACP for video); the ids `grok-cli` and `grok-acp` are internal. Always name the route that ran. Details: [cli-routes.md](references/cli-routes.md).

## Local routes (`cli_media.py`)

`--execute` stays the guard: without it the tool only prints the plan.

| Need | Command |
|---|---|
| Image, a local route VERIFIED | `cli_media.py image --route auto --prompt-file <txt> --output-dir <new> --execute` |
| Image through the user's own Codex or Grok, on request | `cli_media.py image --route codex-cli` (or `grok-cli`) `--prompt-file <txt> --output-dir <new>` dry run, show the consent block, then `--execute` |
| Reference edit | `cli_media.py edit --route grok-cli --reference <img> --prompt-file <txt> --output-dir <new>` |
| Animate an approved still | `cli_media.py video --route auto --reference <img> --prompt-file <txt> --output-dir <new> --duration 6 --resolution 720p --execute` |
| First use, or after a CLI update | `forge_doctor.py --verify-route <route>`, then `--execute` after the user agrees (one quota call); or, with that consent, run the real job on the explicit route (`--route grok-acp` or `codex-cli`, not `auto`, which says NOT_VERIFIED until a proof exists) with `--execute`: its first success records the VERIFIED proof |
| Check a packaged clip by state | video2dsprite `validate_animation.py --require-states` takes the package `--name` (`hero-run`, not `run`) |
| The session cap stopped a route (`CAP`) | ask the user: raise it (`--session-images N`, `FORGE_SESSION_IMAGES`), wait, or take the paid route with consent |
| Timed out or interrupted | `cli_media.py resume --run <id>`, then `--adopt` (never reruns the CLI) |
| Codex made an image that is not in the project | `cli_media.py adopt --codex-thread <thread-id> --output-dir <new>` |
| Many jobs | `cli_media.py batch` ([cli-routes.md](references/cli-routes.md)) |

Ask before an unverified route's one verification call. Never raise the cap or pass `--allow-duplicate` unless the user asks.

## Paid REST (`generate_media.py`)

| Need | Command |
|---|---|
| Plan one image or video and show its consent list | `generate_media.py image` or `video` with `--provider`, `--model`, `--prompt-file`, `--out-dir` (no `--execute`) |
| Send one approved request | the same command plus `--execute` |
| Poll or download a video job without paying again | `generate_media.py resume --job <out>/job.json` |
| Many approved jobs with one consent list | `generate_media.py batch jobs.json`, then `--execute` |

### Consent checklist (every paid call)

1. Run the command without `--execute`. It needs no key, makes no network call and writes nothing.
2. Show the user the plan's `consent` block: provider, model, number of calls and the estimate in USD. If `estimateUsd` is null, say the price is unknown; never guess. Mention `apiHost` when a custom base URL is used, because the API key goes there.
3. Read `warnings`: an existing output folder, an identical earlier request, or a cap that would block.
4. Add `--execute` only after the user approves exactly that provider, model, call count and estimate. Pass the user's limits as `--budget-usd` / `--max-calls`. A host subscription is not API credit.
5. On `submit_unknown`, check the provider's usage history before any new request, then settle the reservation with `media_ledger.py settle`. Never resend an identical request without the user's go-ahead (`--allow-duplicate`).

Keys come only from `OPENAI_API_KEY` / `XAI_API_KEY` in the environment; never put them in prompts, outputs or commits. A Grok sign-in is never an API key. Flags and models: [api-usage.md](references/api-usage.md); providers: [provider-survey.md](references/provider-survey.md); prices: `references/prices.json`.

## Budget and ledger

Every call, local or paid, is a ledger line in the project. `media_ledger.py summary` shows spend and quota use; `media_ledger.py settle <id> --status ...` closes an unknown outcome. Session caps apply to local routes; paid calls have their own caps (`--budget-usd`, `--max-calls`).

## Host notes

- **Codex:** `image_gen` is the host tool, so this skill mostly adopts results (`cli_media.py adopt`) and verifies routes. This skill is explicit-only in Codex.
- **Claude Code:** no media tools; local routes and the paid API are the image and video routes. `<skill-dir>` is `${CLAUDE_SKILL_DIR}`; look at results with Read.
- **Grok:** its native media tools are host tools; the same `grok` executable serves other hosts as the local route. Agent setups: [agent-profiles/README.md](references/agent-profiles/README.md).

## Commands and outputs

Run each tool as one line from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. Write prompts yourself into UTF-8 files; outputs go to a new `--output-dir` (`--out-dir` for `generate_media.py`) inside the project. Results are raw media: hand them to the owning skill for identity, size, alpha, motion and seam checks. Keep prompts, input hashes, job ids and result hashes; report the model requested and the model returned separately. A provider is live-tested only after a real authorized call succeeds. Data contracts: `references/schemas/media.schema.json`.
