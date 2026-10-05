# Local CLI media routes

`scripts/cli_media.py` drives the user's own installed Codex CLI or Grok CLI to make one image,
one reference edit or one image-to-video clip with that CLI's native media tool. These are the
**local agent** routes: they come before the paid REST API, and they spend the **subscription
quota of the user's own sign-in**, never API credit. A host tool you can call yourself (your own
`image_gen`) needs none of this; the paid API route is [`generate_media.py`](api-usage.md).

Run every command from the user's project root. `<skill-dir>` is this skill's folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs, the ledger, run records and proofs stay in the
project, never in the skill folder.

## Route order: local agent first

Images (and reference edits):

1. the host's own image tool (Codex `image_gen`) when you have one;
2. **Codex (local CLI)**: `codex exec` with its native `image_gen` (route id `codex-cli`);
3. **Grok (local CLI)** in one-shot mode, `image_gen` or `image_edit` (route id `grok-cli`);
4. the paid REST API (`generate_media.py`), only with the user's explicit consent for each request;
5. otherwise, explain the gap.

Routes 2 and 3 count only when `forge_doctor.py` reports them **VERIFIED** for the installed CLI
version. Video:

1. **Grok (local CLI)** in ACP mode, `image_to_video` (route id `grok-acp`), only when VERIFIED;
2. the paid REST API with explicit consent;
3. a video the user already has;
4. otherwise, explain the gap.

Tell the user about one "Grok (local CLI)" route: it is the same `grok` executable, run in one-shot
mode for images and in ACP mode for video. The ids `grok-cli` and `grok-acp` are internal (ledger,
proofs, job.json). Code art keeps its envelope on every host: small pixel sprites, FX, tiles and
map data go to codeart2d first.

## Capability check first

Once per session, before choosing an art route:

```bash
python "<skill-dir>/scripts/forge_doctor.py" --host-tools image_gen
```

Pass the media tools in **your own** tool list (`image_gen`, `image_edit`, `image_to_video`, or
`none`); a script cannot see them. The doctor reads no credential file and runs nothing but
local `--version` calls (`--no-exec` skips those too). Use only the routes its `ROUTES` block
names, in the order above; save the report beside your outputs with `--save outputs/doctor.json`.
A route with status `ready` can be used now (a host tool, or a VERIFIED local route within the
session cap); `consent` means the paid API, which needs the user's consent first; `unverified`
local routes are listed but never chosen.

Each local route climbs a readiness ladder: **PRESENT** (the native executable exists; an npm
launcher is resolved to its binary and never run) -> **AUTH_MODE** (sign-in mode known:
`--probe-auth` runs `codex login status`; Grok has no read-only status command) ->
**TOOL_EXPOSED** (the native tool answered a `cli_media.py` run) -> **VERIFIED** (a run of this
exact CLI version and recipe published a verified artifact). Proofs live in
`.forge/route-proofs.json` and are keyed by CLI version and recipe, so a CLI update drops a route
back until it is verified again. An installed CLI is not a connected tool.

To verify a route (one quota call, after the user agrees), and again after every CLI update:

```bash
python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli
python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli --execute
```

The installed CLI version is part of the verification request, so a re-verification after an
update is a new request, and a consented verification is never refused as a duplicate. The
verification makes an image, so it proves `image_gen` only; Grok's `image_edit` becomes VERIFIED
after one successful `cli_media.py edit --route grok-cli ... --execute`.

## When to ask the user

- A VERIFIED local route (`ready`): run it without a per-call question while the session cap
  allows, and always name the route you used ("Codex (local CLI)" or "Grok (local CLI)"). The
  `--execute` flag stays the guard: without it `cli_media.py` only prints the plan.
- An unverified local route: ask before its one verification call.
- The paid REST API: consent for every request (see [api-usage.md](api-usage.md)).
- Never raise the session cap, pass `--allow-duplicate` or `--allow-unverified` unless the user
  asks for it.

## Routes

| Route | Command | Native tool | How it runs |
|---|---|---|---|
| `codex-cli` | `image` | `image_gen` | `codex exec`: read-only sandbox, ephemeral session, user config ignored, web search, shell, plugins, apps, hooks, browser, MCP and sub-agents disabled; prompt on stdin |
| `grok-cli` | `image` | `image_gen` | `grok` one-shot with `--output-format streaming-json`: only the one tool offered, web search and sub-agents off, Bash, WebFetch and MCP denied |
| `grok-cli` | `edit` | `image_edit` | as above; the reference image is copied into the run folder |
| `grok-acp` | `video` | `image_to_video` | `grok agent --no-leader --agent-profile references/agent-profiles/video-agent.md stdio` (ACP): one permission granted, only for the exact image, duration and resolution |
| `auto` | `image`, `edit`, `video` | as chosen | the first route of the order above that is VERIFIED for the installed CLI version |

Every CLI runs in a fresh temporary folder with API keys, other credential-like variables and
the calling agent's own session variables removed from its environment (Grok also gets memory,
managed MCPs, foreign agent rules and its auto-updater switched off). Any other tool call, a
second media call, mismatched arguments, more output than allowed or `--timeout` (default 300 s
for images, 600 s for video) stops it at once. The [agent profile](agent-profiles/video-agent.md)
limits the ACP agent to `image_to_video`.

```bash
python "<skill-dir>/scripts/cli_media.py" image --route auto --prompt-file prompts/hero.txt --output-dir outputs/hero-local
python "<skill-dir>/scripts/cli_media.py" image --route auto --prompt-file prompts/hero.txt --output-dir outputs/hero-local --execute
python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex
python "<skill-dir>/scripts/cli_media.py" image --route grok-cli --prompt-file prompts/forest.txt --output-dir outputs/forest-grok
python "<skill-dir>/scripts/cli_media.py" edit --route grok-cli --reference hero.png --prompt-file prompts/hero-night.txt --output-dir outputs/hero-night
python "<skill-dir>/scripts/cli_media.py" video --route auto --reference hero.png --prompt-file prompts/idle.txt --duration 6 --resolution 720p --output-dir outputs/hero-idle-grok
```

`--route auto` names its choice in `routeChoice` (the route, its label, the order tried and why
earlier routes were skipped), in the printed result and in `job.json`. With no VERIFIED local
route it stops with `NOT_VERIFIED` and runs nothing. A dry run starts no process, so it cannot
read a Grok version: it plans with the newest VERIFIED proof (`verifiedFor`), and `--execute`
checks the installed version again before the call.

## The plan (every run)

Run the command **without** `--execute` first when you need the plan; nothing is started or
written. The plan shows the route and its label, the `consent` block (route, account, one call,
`quota: true`), the exact CLI arguments, the ledger totals with the session usage
(`ledger.session`), and `warnings`: CLI not installed, route not yet verified for this version,
output folder exists, an identical earlier request, or a cap that would block.

## Session cap

The local routes share a session cap, counted in the project's ledger before anything is
started: at most **8 images** (image and edit) and **2 videos** within the last **12 hours** by
default. A call that would exceed it stops with `CAP` and runs nothing. The user can change it
with `--session-images N`, `--session-videos N` and `--session-hours H`, or for a whole session
with `FORGE_SESSION_IMAGES`, `FORGE_SESSION_VIDEOS` and `FORGE_SESSION_HOURS` (a flag wins over
the environment; `0` blocks that kind). Calls that never left the machine (`not_sent`) do not
count; paid REST calls have their own caps in `generate_media.py`.

## Quota and the ledger

Each executed run is reserved in `.forge/ledger.jsonl` before the CLI starts, as a quota call
(`route` `codex-cli`, `grok-cli` or `grok-acp`, `quotaCall: true`, 0 USD), then committed
`done`, `failed`, `unknown` or `not_sent`. Besides the session cap, `--max-calls N` counts paid and
quota calls in the project's ledger and `FORGE_MAX_PAID_REQUESTS` counts paid API calls only. An
identical request that succeeded, is still open or has an unknown outcome is refused unless
`--allow-duplicate`. `python "<skill-dir>/scripts/media_ledger.py" summary` shows the totals and
the session usage.

## A Grok sign-in is never an API key

The REST route (`generate_media.py --provider xai`) uses only `XAI_API_KEY` from the user's xAI
API console. Forge never reads `~/.grok/auth.json` or any other Grok or Codex login file, never
turns a CLI sign-in into a REST credential and never sends one to an API. The local routes run the
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
(paths relative to the file; `route` may be `auto`). The default is a dry-run consent list.
`--execute` runs one job at a time, refuses routes with no proof for the installed CLI version
unless `--allow-unverified`, reuses finished outputs, never retries, stops at the first
account-level or unexpected outcome and writes `<jobs stem>.progress.json`, whose `jobsFile` and
`jobDir` paths are relative to the progress file's folder. The session cap applies to every job.

## Error codes

| Code | Meaning | Ledger | Next step |
|---|---|---|---|
| `INVALID_REQUEST`, `OUTPUT_EXISTS` | bad input, or the output folder exists | nothing reserved | fix the command |
| `DUPLICATE`, `CAP` | identical earlier request, or `--max-calls` / the session cap reached | nothing reserved | reuse it, or ask the user |
| `NOT_INSTALLED`, `NOT_VERIFIED` | no native CLI; `--route auto` or a batch with no VERIFIED route | nothing reserved | install, or verify once with consent |
| `SPAWN_FAILED` | the CLI could not start | `not_sent` | check the install |
| `AUTH_REQUIRED`, `RATE_LIMIT`, `MODERATION` | sign-in, quota or content policy | `failed` | the user logs in, waits, or rewrites |
| `UNEXPECTED_TOOL`, `TOOL_LIMIT`, `PERMISSION_MISMATCH` | the CLI tried something not authorised; it was stopped | `failed` | report it; do not retry blindly |
| `TOOL_UNAVAILABLE`, `GENERATION_FAILED` | the sign-in has no such tool, or the tool failed | `failed` | another route |
| `ARTIFACT_MISSING`, `ARTIFACT_COUNT`, `ARTIFACT_PATH_REJECTED`, `ARTIFACT_STALE`, `ARTIFACT_INVALID` | the output failed a provenance check | `failed` | inspect; `adopt --file` only for a file you checked |
| `TIMEOUT`, `OUTPUT_LIMIT`, `PROTOCOL_ERROR`, `PROVIDER_ERROR`, `PUBLISH_FAILED`, `INTERRUPTED` | outcome unknown; the CLI may have produced something | `unknown` | `resume --run <id>`, then `--adopt` |

A mistyped option exits 2 with the usage line; every other failure prints one
`error: CODE: message` line and exits 1 (130 on Ctrl+C).

## What is verified

The recipes come from the owner's verified runs (Codex CLI 0.153.4 image, Grok image and ACP
video, September 2026) and are tested here only against fake CLIs. CLI flags and event formats
change between versions: the first run of each route on a new version is its verification, which
is why proofs are version-keyed. Outputs are raw media: process and check them with the sprite,
video or map skill.
