# route_media.py: one command for every generated image or clip

`scripts/route_media.py` is the single entry point the Forge skills (and their pipeline scripts)
call for generated art. It picks the route, sends the request, and prints one JSON line. The
lower-level tools stay available: [`generate_media.py`](api-usage.md) for the paid APIs and
[`cli_media.py`](cli-routes.md) for the local CLIs.

## Route order (owner decision 2026-10-06)

1. **API**, when a key is configured. Images: OpenAI (`gpt-image-2.5-sunburst`), then xAI
   (`grok-imagine-image-2.0`). Video: xAI (`grok-imagine-video-1.5`). A configured key is the
   owner's consent: the request is sent at once, with no per-call question.
2. **Local**: the user's own signed-in CLI (subscription quota, never an API key). Images: Codex
   (`codex exec` with its native `image_gen`, reference images attached), then Grok one-shot
   (`image_gen`, or `image_edit` of one reference). Video: Grok in ACP mode (`image_to_video`).
   An installed CLI is used as is; its first successful run records the VERIFIED proof for that
   CLI version. An agent that has its own image tool (Codex `image_gen`, Grok's native tools) and
   no configured key may use that tool instead and adopt the result.
3. **No route**: `{"status":"no-route","fallback":"codeart2d"}` and exit 3. codeart2d draws code art
   only then, or when the user explicitly asks for code-drawn art.

`--route auto` (default) walks that order. `--route api` or `--route local` keeps one group;
`openai`, `xai`, `codex-cli`, `grok-cli` or `grok-acp` names one route. A route that cannot take
the request is skipped before anything runs: xAI and Grok (local CLI) take one reference image,
Codex up to 8, OpenAI up to 16; Grok (local CLI) video renders 480p or 720p.

Within auto and a group, a route whose **account** cannot serve the request passes it on to the
next route: API `no_key`, `auth`, `quota`, `rate_limit`, `entitlement`, `not_sent`; local
`NOT_INSTALLED`, `SPAWN_FAILED`, `AUTH_REQUIRED`, `RATE_LIMIT`, `TOOL_UNAVAILABLE`,
`GENERATION_FAILED`. A refused API attempt's folder is kept beside the output as
`<out-dir>.failed-<route>` (its `job.json` says why). Every other failure stops at once, so a
moderation refusal is never retried elsewhere and an outcome that may have cost money is never
repeated.

## Commands

```bash
python "<skill-dir>/scripts/route_media.py" resolve --kind image
python "<skill-dir>/scripts/route_media.py" resolve --kind image --references 2
python "<skill-dir>/scripts/route_media.py" image --prompt-file prompts/master.txt --reference art/identity.png --reference art/peer.png --size 1024x1024 --out-dir outputs/hero-master
python "<skill-dir>/scripts/route_media.py" video --prompt-file prompts/idle.txt --reference art/hero-master.png --last-frame art/hero-master.png --duration 6 --resolution 720p --out-dir outputs/hero-idle
python "<skill-dir>/scripts/route_media.py" video --prompt-file prompts/run.txt --reference art/hero-master.png --out-dir outputs/hero-run --dry-run
```

| Option | Meaning |
|---|---|
| `--prompt-file` | UTF-8 prompt the agent wrote. Local routes take the size and every instruction from it |
| `--reference` | image: repeatable, in the order the prompt names them; video: the first frame (the approved still) |
| `--size` | image: `WIDTHxHEIGHT` or `auto` (default `1024x1024`); xAI gets the nearest aspect ratio and 1k or 2k |
| `--last-frame` | video: pins the end frame where the route can (xAI `grok-imagine-video-1.5` at 480p or 720p, same canvas as the first frame); the result says `lastFrameUsed` |
| `--duration`, `--resolution` | video: 1..15 seconds (default 6); `480p`, `720p` (default) or `1080p` |
| `--out-dir` | a new folder: `generated.<ext>`, `job.json`, `prompt.txt`; an existing folder is refused |
| `--route` | `auto` (default), `api`, `local` or one route |
| `--project-dir` | project root holding `.forge/` (ledger, run records, proofs); default the current folder |
| `--dry-run` | print the plan and estimate of the route that would run; no key is used, nothing is sent or written |
| `--budget-usd`, `--max-calls`, `--timeout`, `--purpose` | opt-in caps, the route's time limit, receipt text |

## Output

Success, exit 0 (one ASCII JSON line; the first five keys are the contract):

```json
{"status":"ok","route":"api:openai","artifact":"outputs/hero-master/generated.png","sha256":"<64 hex>","estimateUsd":null,"kind":"image","label":"OpenAI API (gpt-image-2.5-sunburst)","model":"gpt-image-2.5-sunburst","job":"outputs/hero-master/job.json"}
```

- `route` is `api:openai`, `api:xai`, `local:codex-cli`, `local:grok-cli` or `local:grok-acp`.
- `estimateUsd` is the list-price estimate from [prices.json](prices.json), `null` when no verified
  price row exists, `0.0` for a local route (subscription quota).
- Video adds `lastFrameUsed`; `notes` explains a dropped last frame; `attempts` lists earlier
  routes that refused the request (`route`, `code`, `message`, `keptIn`).
- `resolve` prints `{"status":"ok","route":...,"order":[...],"available":[...],"skipped":[...]}`
  (video adds `pinsLastFrame`); `--dry-run` prints `"status":"dry-run"` with the estimate.

No route, exit 3: `{"status":"no-route","fallback":"codeart2d","kind":...,"skipped":[...]}`. A
failure prints one `error: <route>: <code>: <message>` line and exits 1; a usage error exits 2;
Ctrl+C exits 130.

## Keys and the user config file

Keys come from `OPENAI_API_KEY` and `XAI_API_KEY` in the environment, or from one JSON file in
the user's own configuration folder, never inside a project:

| System | File |
|---|---|
| Windows | `%APPDATA%\agent-sprite-forge\config.json` |
| macOS, Linux | `$XDG_CONFIG_HOME/agent-sprite-forge/config.json`, default `~/.config/agent-sprite-forge/config.json` |

```json
{
  "OPENAI_API_KEY": "sk-...",
  "XAI_API_KEY": "xai-...",
  "models": {"openai-image": "gpt-image-2.5-sunburst", "xai-image": "grok-imagine-image-2.0", "xai-video": "grok-imagine-video-1.5"}
}
```

Every field is optional; the environment wins over the file. On macOS and Linux keep the file
private (`chmod 600`; the doctor warns otherwise). Keys are read in-process only: they are never
printed, logged, written, passed on a command line or given to a child process, and a Codex or
Grok CLI sign-in is never used as an API key. `forge_doctor.py` reports only which providers are
configured (`apiKeys`, yes or no).

## Spend records, no caps by default

Every call, paid or local, is a line in `<project>/.forge/ledger.jsonl` with its estimate, and
each output folder keeps `job.json` (route, model, prompt hash, reference hashes, receipt). There
is no cap unless one is asked for: `--budget-usd`, `--max-calls`, `FORGE_MAX_PAID_REQUESTS`, and
for the local routes `FORGE_SESSION_IMAGES` / `FORGE_SESSION_VIDEOS` (window
`FORGE_SESSION_HOURS`, default 12). An identical earlier request is not refused: a new
`--out-dir` is a new take, and `receipt.attempt` counts the takes.
`python "<skill-dir>/scripts/media_ledger.py" summary` shows the totals.

## Test seam

When `FORGE_ROUTE_MEDIA_FAKE` names a script, `route_media.py` parses and checks the command as
usual (files exist, `--out-dir` is new, values in range) and then runs `python <script> <the same
arguments>`, returning the script's output and exit code unchanged. Tests of the skills that call
`route_media.py` use it to fake generation: the fake writes `generated.png` or `generated.mp4`
into `--out-dir` and prints the success line, or prints the no-route line and exits 3.
