# CLI media routes (opt-in)

`scripts/cli_media.py` drives the user's own installed Codex CLI or Grok Build CLI to make one
image, one reference edit or one image-to-video clip with that CLI's native media tool. These
routes spend the **subscription quota of the user's own sign-in**. They are never automatic:
use one only when the user asks for it, or approves it after you show the plan. A host tool you
can call yourself (your own `image_gen`) needs none of this; a paid API route is
[`generate_media.py`](api-usage.md).

Run every command from the user's project root. `<skill-dir>` is this skill's folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs, the ledger, run records and proofs stay in the
project, never in the skill folder.

## Capability check first

Once per session, before choosing an art route:

```bash
python "<skill-dir>/scripts/forge_doctor.py" --host-tools image_gen
```

Pass the media tools in **your own** tool list (`image_gen`, `image_edit`, `image_to_video`, or
`none`); a script cannot see them. The doctor reads no credential file and runs nothing but
local `--version` calls (`--no-exec` skips those too). Use only the routes its `ROUTES` block
names; save the report beside your outputs with `--save outputs/doctor.json`.

Each CLI route climbs a readiness ladder: **PRESENT** (the native executable exists; an npm
launcher is resolved to its binary and never run) -> **AUTH_MODE** (sign-in mode known:
`--probe-auth` runs `codex login status`; Grok has no read-only status command) ->
**TOOL_EXPOSED** (the native tool answered a `cli_media.py` run) -> **VERIFIED** (a run of this
exact CLI version and recipe published a verified artifact). Proofs live in
`.forge/route-proofs.json` and are keyed by CLI version and recipe, so a CLI update drops a route
back until it is verified again. An installed CLI is not a connected tool: `ROUTES` offers a CLI
route only when it is VERIFIED; until then it is listed as an `unverified` option.

To verify a route once (one quota call, after the user agrees):

```bash
python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli
python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli --execute
```

## Routes

| Route | Command | Native tool | How it runs |
|---|---|---|---|
| `codex-cli` | `image` | `image_gen` | `codex exec`: read-only sandbox, ephemeral session, user config ignored, web search, shell, plugins, apps, hooks, browser, MCP and sub-agents disabled; prompt on stdin |
| `grok-cli` | `image` | `image_gen` | `grok` headless with `--output-format streaming-json`: only the one tool offered, web search and sub-agents off, Bash, WebFetch and MCP denied |
| `grok-cli` | `edit` | `image_edit` | as above; the reference image is copied into the run folder |
| `grok-acp` | `video` | `image_to_video` | `grok agent --no-leader --agent-profile references/agent-profiles/video-agent.md stdio` (ACP): one permission granted, only for the exact image, duration and resolution |

Every CLI runs in a fresh temporary folder with API keys, other credential-like variables and
the calling agent's own session variables removed from its environment (Grok also gets memory,
managed MCPs, foreign agent rules and its auto-updater switched off). Any other tool call, a
second media call, mismatched arguments, more output than allowed or `--timeout` (default 300 s
for images, 600 s for video) stops it at once. The [agent profile](agent-profiles/video-agent.md)
limits the ACP agent to `image_to_video`.

```bash
python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex
python "<skill-dir>/scripts/cli_media.py" image --route grok-cli --prompt-file prompts/forest.txt --output-dir outputs/forest-grok
python "<skill-dir>/scripts/cli_media.py" edit --route grok-cli --reference hero.png --prompt-file prompts/hero-night.txt --output-dir outputs/hero-night
python "<skill-dir>/scripts/cli_media.py" video --route grok-acp --reference hero.png --prompt-file prompts/idle.txt --duration 6 --resolution 720p --output-dir outputs/hero-idle-grok
```

## Consent (every run)

1. Run the command **without** `--execute`. Nothing is started or written. The plan shows the
   `consent` block (route, account, one call, `quota: true`), the exact CLI arguments and
   `warnings`: CLI not installed, route not yet verified for this version, output folder
   exists, an identical earlier request, or a call cap that would block.
2. Tell the user: one call on the subscription quota of their own Codex (ChatGPT) or Grok
   sign-in; not API credit and not free. Say so when the route is not verified yet.
3. Add `--execute` only after the user agrees. Pass their limit as `--max-calls`.

## Quota and the ledger

Each executed run is reserved in `.forge/ledger.jsonl` before the CLI starts, as a quota call
(`route` `codex-cli`, `grok-cli` or `grok-acp`, `quotaCall: true`, 0 USD), then committed
`done`, `failed`, `unknown` or `not_sent`. `--max-calls N` counts paid and quota calls in the
project's ledger; `FORGE_MAX_PAID_REQUESTS` counts paid API calls only. An identical request
that succeeded, is still open or has an unknown outcome is refused unless `--allow-duplicate`.
`python "<skill-dir>/scripts/media_ledger.py" summary` shows the totals.

## A Grok sign-in is never an API key

The REST route (`generate_media.py --provider xai`) uses only `XAI_API_KEY` from the user's xAI
API console. Forge never reads `~/.grok/auth.json` or any other Grok or Codex login file, never
turns a CLI sign-in into a REST credential and never sends one to an API. The CLI routes run the
user's own CLI, which uses its own sign-in, and their children receive no API key. If the user
wants API billing, use `generate_media.py`; if they want their subscription, use these routes.

## Provenance

- The run record `.forge/cli-runs/<run>.json` (a `job_v2` document) is written before the CLI
  starts; the Codex thread id or Grok session id is saved the moment the CLI reports it.
- The artifact is taken only from the CLI's own folder for that run:
  `CODEX_HOME/generated_images/<thread>/` or `GROK_HOME/sessions/*/<session>/images|videos/`.
  It must sit there without a symlink or junction, be a regular file of a sane size, be newer
  than the run, be the only output, carry PNG/JPEG/WebP (image) or MP4 (video) magic bytes and
  decode. A Codex final answer naming a different file is refused.
- It is copied with an exclusive create and its sha256 checked, into a staged `--output-dir`
  that is published only when every check passes: `generated.<ext>`, `job.json` (route, CLI
  version, recipe, receipt, provenance, ledger reservation) and `prompt.txt`. A failed run
  leaves no output folder; its run record says why.

## Resume and adopt

```bash
python "<skill-dir>/scripts/cli_media.py" resume --run <run-id>
python "<skill-dir>/scripts/cli_media.py" resume --run <run-id> --adopt
python "<skill-dir>/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir outputs/hero-desktop
```

- `resume` inspects an interrupted, timed-out or failed run; `--adopt` publishes its output
  after the same checks **without running the CLI again**, and settles the ledger reservation
  as `done`. `--file NAME` picks one image when the folder holds several; `--output-dir` picks a
  new folder.
- `adopt --codex-thread` copies an image that Codex Desktop or an interactive Codex session
  generated: from the thread's `generated_images` folder, or, where the host keeps no PNG, from
  the image inline in the thread's session rollout (`--index N` when it holds several). It makes
  no call and writes no ledger line. It replaces `save_imagegen_result.py` (PR #5) and covers
  issue #4.

## Batch

```bash
python "<skill-dir>/scripts/cli_media.py" batch jobs.json
python "<skill-dir>/scripts/cli_media.py" batch jobs.json --execute --max-calls 10
```

`jobs.json` holds `{"jobs": [{"id", "command", "route", "prompt_file", "output_dir", ...}]}`
(paths relative to the file). The default is a dry-run consent list. `--execute` runs one job at
a time, refuses routes with no proof for the installed CLI version unless `--allow-unverified`,
reuses finished outputs, never retries, stops at the first account-level or unexpected outcome
and writes `<jobs stem>.progress.json`.

## Error codes

| Code | Meaning | Ledger | Next step |
|---|---|---|---|
| `INVALID_REQUEST`, `OUTPUT_EXISTS` | bad input, or the output folder exists | nothing reserved | fix the command |
| `DUPLICATE`, `CAP` | identical earlier request, or `--max-calls` reached | nothing reserved | reuse it, or ask the user |
| `NOT_INSTALLED`, `NOT_VERIFIED` | no native CLI; batch on an unverified route | nothing reserved | install, or verify once |
| `SPAWN_FAILED` | the CLI could not start | `not_sent` | check the install |
| `AUTH_REQUIRED`, `RATE_LIMIT`, `MODERATION` | sign-in, quota or content policy | `failed` | the user logs in, waits, or rewrites |
| `UNEXPECTED_TOOL`, `TOOL_LIMIT`, `PERMISSION_MISMATCH` | the CLI tried something not authorised; it was stopped | `failed` | report it; do not retry blindly |
| `TOOL_UNAVAILABLE`, `GENERATION_FAILED` | the sign-in has no such tool, or the tool failed | `failed` | another route |
| `ARTIFACT_MISSING`, `ARTIFACT_COUNT`, `ARTIFACT_PATH_REJECTED`, `ARTIFACT_STALE`, `ARTIFACT_INVALID` | the output failed a provenance check | `failed` | inspect; `adopt --file` only for a file you checked |
| `TIMEOUT`, `OUTPUT_LIMIT`, `PROTOCOL_ERROR`, `PROVIDER_ERROR`, `PUBLISH_FAILED`, `INTERRUPTED` | outcome unknown; the CLI may have produced something | `unknown` | `resume --run <id>`, then `--adopt` |

## What is verified

The recipes come from the owner's verified runs (Codex CLI 0.153.4 image, Grok Build image and
ACP video, September 2026) and are tested here only against fake CLIs. CLI flags and event
formats change between versions: the first run of each route on a new version is its
verification, which is why proofs are version-keyed. Outputs are raw media: process and
check them with the sprite, video or map skill.
