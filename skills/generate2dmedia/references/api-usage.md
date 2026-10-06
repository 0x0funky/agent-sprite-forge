# API adapter usage

Requires Python 3.10+ and Pillow. HTTP uses the standard library, with no provider
SDK dependency. Run commands from the user's project root; `<skill-dir>` is this
skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code). Outputs and the spend ledger
stay in the project, never in the skill folder.

Skills reach this adapter through [`route_media.py`](route-media.md): when a key is
configured, the API is the first route and the configured key is the owner's
consent, so `route_media.py` sends the request at once (it passes `--execute`).
Run directly, `generate_media.py` keeps its dry run: every command is a dry run
until `--execute` is added; a dry run needs no key, makes no network call and
writes nothing.

Keys come from `OPENAI_API_KEY` / `XAI_API_KEY` in the environment, else from the
user config file (`%APPDATA%\agent-sprite-forge\config.json` on Windows,
`~/.config/agent-sprite-forge/config.json` elsewhere; see
[route-media.md](route-media.md)). They are read in-process and never printed,
logged or written; this tool doesn't search the filesystem for other credentials
or load `.env` files.

Console output is one line of ASCII JSON (non-ASCII text is `\u` escaped), and errors
are a single `error: ...` line on stderr with exit code 1, so legacy Windows code
pages (cp1252, cp950) never turn a finished paid job into a failure.

## The plan of a paid call

Through `route_media.py` a configured key is consent and the estimate is printed with
the result (`estimateUsd`; `--dry-run` shows the plan first). Run directly, the
command without `--execute` prints the plan:

- `consent`: `provider`, `model`, `calls` (always 1 per job) and `estimateUsd`, plus
  `apiHost`, the host that will receive the API key.
- `estimate`: `usd`, `basis` (the price rows used) and `pricesVersion`. `usd` is
  `null` when no verified price row matches the request, for example any OpenAI
  model or an unlisted resolution. Say "price unknown" in that case; never guess.
- `ledger`: calls and USD already recorded in this project; `warnings`: an existing
  output folder, an identical earlier request, or a cap that `--execute` would hit.

Report the provider, model, number of calls and the estimate (or "unpriced") with
every result. A subscription to a host app is not evidence of API access or API
credit.

## Images

```bash
python "<skill-dir>/scripts/generate_media.py" image --provider openai --model gpt-image-2.5-sunburst --prompt-file hero.txt --size 1024x1024 --transparent --out-dir outputs/hero-api
python "<skill-dir>/scripts/generate_media.py" image --provider openai --model gpt-image-2.5-sunburst --reference hero.png --prompt-file attack.txt --out-dir outputs/attack-api
python "<skill-dir>/scripts/generate_media.py" image --provider xai --model grok-imagine-image-2.0 --reference forest.png --prompt-file forest-night.txt --resolution 2k --quality medium --out-dir outputs/forest-api
```

- Add `--execute` to send one paid request. Image count is fixed to one.
- OpenAI supports repeated `--reference` inputs in this adapter (up to 16). Edits
  are multipart; generations are JSON. Outputs request PNG. Transparency and size
  remain subject to the chosen model's actual capabilities and account access.
- xAI edits use JSON, **not** OpenAI's multipart edit protocol. This adapter accepts
  one reference. xAI multi-image editing exists but is not implemented here.
- xAI supports `--resolution 1k|2k`, optional `--aspect-ratio`, and image-2.0
  `--quality low|medium|auto`. It has no transparent-background switch here.
  Choose a keyed backdrop in the prompt if required; do not key a returned native
  RGBA image again. OpenAI optionally accepts `--quality low|medium|high|auto`.
- PNG/JPEG/WebP references are decoded to verify actual type before upload; maximum
  20 MiB per input and 40 MiB combined (adapter limits, not claims about vendor limits).
- `--submit-timeout` (default 300 s, at most 600) bounds the wait for the paid POST;
  `--timeout` also caps it. OpenAI documents up to about two minutes for complex
  prompts. Each paid request carries an `X-Client-Request-Id` (stored as
  `clientRequestId`), and the provider's `x-request-id` is stored as
  `providerRequestId` for support questions.

## Videos and resume

```bash
python "<skill-dir>/scripts/generate_media.py" video --provider xai --model grok-imagine-video-1.5 --reference hero.png --prompt-file idle.txt --duration 4 --resolution 720p --last-frame hero.png --out-dir outputs/idle-api --execute
python "<skill-dir>/scripts/generate_media.py" resume --job outputs/idle-api/job.json --timeout 600
```

Models supported here: `grok-imagine-video` (480p/720p),
`grok-imagine-video-1.5` and `grok-imagine-video-1.5-lite` (also 1080p).
First/last pinning is implemented only for full 1.5 at 480p/720p because it switches
to reference-video mode. First/last canvas sizes must match. No video aspect override
is sent, avoiding stretched heroes. Full 1.5 requests silent output; other models'
audio can be stripped during packaging. No keyframes/edit/extend endpoints yet.

`--timeout` defaults to 600 seconds for polling; each HTTP operation is bounded.
`--poll-interval` defaults to 5 seconds. A network error leaves the job in place.
Known video IDs can be polled again without another paid generation. Terminal
`failed`/`expired` jobs aren't automatically replaced, and a video the provider
withheld after moderation is terminal too. Jobs written by the previous version
(`schemaVersion` 1) still resume.

## Outcomes: what each job status means

| `status` in job.json | Meaning | Next step |
| --- | --- | --- |
| `done` | Artifact published and hashed | Hand it to the owning skill for QA |
| `not_sent` | DNS failure, refused or failed connection: nothing left the machine | Fix the network, then rerun with a new `--out-dir` |
| `failed` | The provider rejected the request (HTTP 4xx), failed or expired the job, or withheld it after moderation | Read `error`; change the request; never resend it unchanged |
| `submit_unknown` (or `submitting` left by a crash) | The request may have reached the provider (timeout after sending, HTTP 5xx, unreadable response) | Check the provider's usage history, then settle the reservation (below) before any new request |
| `pending`, `pending_timeout`, `interrupted` (video) | The provider job may still finish | `resume`; it only polls and downloads |
| `interrupted` with `partialArtifact` | Returned bytes could not be published; they are kept in the named `.partial` file | Recover the file; do not pay again |

Provider errors keep only whitelisted, scrubbed fields in `job.json` `error` and on
stderr: `httpStatus`, `code`, `type`, `param`, `message` (at most 300 characters,
with keys, URLs and long tokens removed) and `requestId`. `receipt.outcomeCode`
names the outcome: `ok`, `not_sent`, `submit_unknown`, `auth`, `quota`,
`rate_limit`, `moderation`, `entitlement`, `invalid_request`, `provider_error`,
`bad_response`, `partial_artifact`, `pending_timeout`, `failed`, `expired` and others.

## Spend ledger, opt-in caps and the duplicate guard

Every `--execute` first appends a `reserved` line to `<project>/.forge/ledger.jsonl`
(`--project-dir`, default: the current folder) and later commits the outcome:
`done`, `failed`, `unknown` or `not_sent`. The file is append-only; the last line per
`reservationId` wins. A `reserved` or `unknown` reservation keeps holding its
estimate, so a timeout can never free budget that may have been spent.

There is no cap unless the user sets one (owner decision 2026-10-06):

- `--budget-usd X` refuses to send when recorded + held + this estimate exceeds X.
  An unpriced request cannot be checked against a USD budget and is refused; use
  `--max-calls` for it, or pass `--prices` with a verified row.
- `--max-calls N` refuses to send when the ledger already holds N calls (paid API
  calls and subscription quota calls both count).
- `FORGE_MAX_PAID_REQUESTS=N` in the environment caps paid API calls for every
  command, even when a flag is forgotten. `0` blocks all paid calls.
- Caps are cumulative over the project's ledger. To allow more, raise the cap
  consciously; never edit or delete ledger lines.
- Run directly, an identical request (same provider, endpoint, model, options,
  prompt hash and reference hashes) that already succeeded, is still open or has an
  unknown outcome is refused. Reuse the earlier job, or pass `--allow-duplicate` to
  pay again; `receipt.attempt` then counts the attempts. `route_media.py` passes
  `--allow-duplicate`: its new `--out-dir` is a deliberate new take.

Settle an `unknown` reservation once the provider's usage history shows the truth:

```bash
python "<skill-dir>/scripts/media_ledger.py" summary
python "<skill-dir>/scripts/media_ledger.py" settle <reservation-id> --status failed
```

`settle` accepts `done` (charged; add `--actual-usd` when known), `failed` (not
charged) or `not_sent`. Estimates use [prices.json](prices.json): list prices with a
`source` URL and a `verifiedAt` date for each row. They exclude tax and rejected
attempts; re-verify them before a large batch.

## Batch

```bash
python "<skill-dir>/scripts/generate_media.py" batch jobs.json
python "<skill-dir>/scripts/generate_media.py" batch jobs.json --execute --workers 2 --budget-usd 5
```

A jobs file is a JSON list (or `{"jobs": [...]}`). Each job has an `id`, a `command`
(`image` or `video`) and the same options as the CLI, written as keys; paths are
relative to the jobs file:

```json
{"jobs": [
  {"id": "slime-idle", "command": "video", "provider": "xai", "model": "grok-imagine-video-1.5-lite", "prompt_file": "prompts/slime-idle.txt", "reference": ["art/slime.png"], "duration": 4, "out_dir": "jobs/slime-idle"},
  {"id": "crate", "command": "image", "provider": "xai", "model": "grok-imagine-image-2.0", "prompt_file": "prompts/crate.txt", "resolution": "1k", "quality": "low", "out_dir": "jobs/crate"}
]}
```

- Without `--execute` the batch validates every job and prints the consent list:
  one row per job with provider, model, calls and estimate, plus totals. It sends
  and writes nothing.
- `--execute` runs at most `--workers` (1 or 2) jobs at once and rewrites the
  progress file (`jobs.progress.json` beside `jobs.json`, or `--progress`) after
  every job.
- A job whose output folder already holds a verified `done` result is reused; any
  other existing folder is left for a human. Nothing is ever retried.
- Dispatch stops on anything that is not specific to one job: auth, quota or rate
  limit, moderation, entitlement, caps, a missing key, network failures, and any
  outcome that may have cost money without a result. In-flight jobs finish;
  Ctrl+C also stops dispatching.
- Budget, call caps, `--allow-duplicate`, `--prices` and `--project-dir` are set once
  for the whole batch.

## Custom endpoints and xAI zero data retention

`--base-url https://gateway.example/v1` replaces the provider's base URL. It sends
the API key to that host, so it is https-only and requires `--allow-custom-base-url`
(also on `resume` for such a job). `--upload-url` adds xAI's `upload_url` field for
zero-data-retention teams; the REST field name is not verified against a live
account. The upload URL may be signed, so job.json stores only its host and hash.

## Outputs and verification

```text
<out>/prompt.txt       exact submitted prompt
<out>/job.json         schemaVersion 2: request plan, estimate, fingerprint, consent,
                       receipt (timing, attempt, purpose, toolVersion, outcomeCode),
                       ledger reservation, request IDs, error fields, output hash
<out>/generated.png    or .jpg/.webp based on actual image bytes
<out>/generated.mp4    raw video, still requiring decode/motion/alpha QA
<project>/.forge/ledger.jsonl   append-only spend ledger shared by every job
```

No API keys, base64 blobs, raw provider error bodies or signed URLs are logged; the
key is also refused if it appears in the prompt. `returnedModel` can be null: a
requested model is not evidence of the actual serving revision. MP4 header
validation only detects obvious non-video downloads; it is not decode verification.
Resume verifies the hash of already completed files. Paid bytes are never deleted:
publication is a hard link, or an exclusive write plus fsync on volumes without hard
links (exFAT, FAT32, some network drives), and the temporary copy is removed only
after the published file's sha256 matches.

The adapter refuses authenticated redirects and does not attach credentials to
media downloads. Downloads also reject redirects; if the provider starts returning
a redirecting media host, implement a bounded HTTPS-only redirect policy **without
forwarding authorization** and test it before enabling it.

## Extending providers

Keep generation state separate from asset manifests. Add a request builder, an
explicit capability gate, bounded status/download handling, a verified price row and
mocked contract tests. Preserve user-selected models; do not silently fall back to a
different provider. Add a live result to the verification record only after a real
call.
