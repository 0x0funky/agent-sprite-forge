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

## Providers and keys

| `--provider` | Key (environment or the user config file) | `image` | `video` |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | yes | no |
| `gemini` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` (`GOOGLE_API_KEY` wins) | yes | no |
| `xai` | `XAI_API_KEY` | yes | yes |
| `byteplus` | `ARK_API_KEY` | yes (Seedream) | yes (Seedance) |
| `fal` | `FAL_KEY` | reference edits | yes |

Keys come from the environment, else from the user config file
(`%APPDATA%\agent-sprite-forge\config.json` on Windows,
`~/.config/agent-sprite-forge/config.json` elsewhere; see
[route-media.md](route-media.md)). They are read in-process and never printed,
logged or written; this tool doesn't search the filesystem for other credentials
or load `.env` files, and a provider never uses another provider's key.

Each adapter lives in `scripts/media_providers.py` behind one interface: capability
gating, request building, the submit answer, the status poll, the media download and
error mapping. Each model's limits and doc URLs are in
[capabilities.json](capabilities.json) (verified 2026-10-06 against the vendors'
documentation, not by a live call). `python "<skill-dir>/scripts/media_providers.py" list`
prints them.

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
  `null` when no verified price row matches the request, for example OpenAI at
  quality `auto`. Say "price unknown" in that case; never guess.
- `capability`: the model record's `verifiedAt` and doc URLs; `notes`: what the
  adapter adapted or warns about (a retiring model, a raised size, padding).
- `ledger`: calls and USD already recorded in this project; `warnings`: an existing
  output folder, an identical earlier request, or a cap that `--execute` would hit.

Report the provider, model, number of calls and the estimate (or "unpriced") with
every result. A subscription to a host app is not evidence of API access or API
credit.

## Images

```bash
python "<skill-dir>/scripts/generate_media.py" image --provider openai --model gpt-image-2.5-sunburst --prompt-file hero.txt --size 1024x1024 --quality high --transparent --out-dir outputs/hero-api
python "<skill-dir>/scripts/generate_media.py" image --provider openai --model gpt-image-2.5-sunburst --reference hero.png --prompt-file attack.txt --out-dir outputs/attack-api
python "<skill-dir>/scripts/generate_media.py" image --provider gemini --model gemini-3-pro-image --reference hero.png --reference peer.png --prompt-file master.txt --size 1024x1024 --out-dir outputs/master-gemini
python "<skill-dir>/scripts/generate_media.py" image --provider xai --model grok-imagine-image-2.0 --reference forest.png --prompt-file forest-night.txt --resolution 2k --quality medium --out-dir outputs/forest-api
python "<skill-dir>/scripts/generate_media.py" image --provider byteplus --model dola-seedream-5-0-pro-260628 --reference hero.png --prompt-file master.txt --size 1024x1024 --out-dir outputs/master-seedream
python "<skill-dir>/scripts/generate_media.py" image --provider fal --model fal-ai/nano-banana-2/edit --reference hero.png --prompt-file master.txt --size 1024x1024 --out-dir outputs/master-fal
```

- Add `--execute` to send one paid request. Image count is fixed to one.
- **OpenAI**: up to 16 references (multipart `image[]` edits); generations are JSON.
  Output is PNG. `--transparent` asks for a native transparent background. Sizes are
  `auto` or `WIDTHxHEIGHT` with both sides multiples of 16, an aspect of 1:3 to 3:1
  and 655,360 to 8,294,400 pixels. `--quality low|medium|high|auto` (2.5 adds
  `xhigh` and `max`). `gpt-image-1` is refused (it ends 2026-10-23).
- **Gemini**: up to 14 references, sent as inline parts after the prompt, in order;
  `--size` becomes the nearest aspect ratio and `1K`/`2K`/`4K` (or pass
  `--aspect-ratio` and `--resolution 512|1k|2k|4k`). No transparency and no quality
  switch: choose the model instead (`gemini-3.1-flash-lite-image`,
  `gemini-3.1-flash-image`, `gemini-3-pro-image`). Interim "thought" images are
  skipped; a safety block is a `moderation` failure. Images carry SynthID.
- **xAI**: JSON edits, never OpenAI's multipart; `grok-imagine-image-2.0` takes up to
  5 references, the classic model 1. `--resolution 1k|1.5k|2k`, optional
  `--aspect-ratio`, image-2.0 `--quality low|medium|auto`; no transparency switch.
  Choose a keyed backdrop in the prompt; do not key a returned native RGBA image
  again.
- **BytePlus Seedream**: up to 10 references (data URIs); `--size WxH` within
  921,600 to 4,624,220 pixels (a smaller size is raised, with a note). ASF sends
  `watermark: false` (on by default) and asks for PNG and base64.
- **fal.ai**: reference edits only (`fal-ai/nano-banana-2/edit`,
  `fal-ai/nano-banana-pro/edit`, `bytedance/seedream/v5/pro/edit`,
  `openai/gpt-image-2/edit`). The references are uploaded to the fal CDN first (free
  requests, before the paid one); `--provider-option fal_upload=data` sends data URIs.
- `--provider-option KEY=VALUE` adds a request field the adapter does not set, for
  example `moderation=low` (OpenAI), `generationConfig.seed=7` (Gemini; dots nest) or
  `safety_tolerance="5"` (fal.ai Veo; VALUE is JSON when it parses). Fields ASF sets
  itself (model, prompt, images) are refused.
- PNG/JPEG/WebP references are decoded to verify their type before upload; maximum
  20 MiB per input and 40 MiB combined (adapter limits, not claims about vendor limits).
- `--submit-timeout` (default 300 s, at most 600) bounds the wait for the paid POST;
  `--timeout` also caps it. OpenAI documents up to about two minutes for complex
  prompts. Each paid request carries an `X-Client-Request-Id` (stored as
  `clientRequestId`), and the provider's request id header (`x-request-id`,
  `x-fal-request-id`) is stored as `providerRequestId` for support questions.

## Videos and resume

```bash
python "<skill-dir>/scripts/generate_media.py" video --provider xai --model grok-imagine-video-1.5 --reference hero.png --prompt-file idle.txt --duration 6 --resolution 720p --last-frame hero.png --keyframe hero-mid.png@3 --out-dir outputs/idle-api --execute
python "<skill-dir>/scripts/generate_media.py" video --provider byteplus --model dreamina-seedance-2-0-260128 --reference hero.png --last-frame hero.png --prompt-file idle.txt --duration 6 --out-dir outputs/idle-seedance --execute
python "<skill-dir>/scripts/generate_media.py" video --provider fal --model fal-ai/kling-video/v3/pro/image-to-video --reference hero.png --last-frame hero.png --prompt-file idle.txt --duration 6 --out-dir outputs/idle-kling --execute
python "<skill-dir>/scripts/generate_media.py" resume --job outputs/idle-api/job.json --timeout 600
```

- **xAI**: `grok-imagine-video-1.5` takes `--last-frame` and up to 4 `--keyframe
  PATH@SECONDS` (between 0 and the duration, at least 1/3 s apart, same canvas as
  the first frame) at 480p or 720p, and is asked for silent output
  (`generate_audio: false`). `grok-imagine-video-1.5-lite` is the draft model; the
  classic `grok-imagine-video` renders 480p or 720p. No aspect override is sent, so
  the clip follows the still.
- **BytePlus Seedance**: first and last frames as `role: first_frame` / `last_frame`
  (identical frames are allowed), `ratio: "1:1"` for a square still (2.5 follows the
  still), 4 to 15 s, `generate_audio: false`, no watermark. First frames must be 300
  to 6000 px per side.
- **fal.ai**: one curated parameter map per model in
  [capabilities.json](capabilities.json): Kling v3 (`start_image_url`,
  `end_image_url`), Veo 3.1 first-last (both frames required; 16:9 only, so ASF pads
  the still onto a 16:9 canvas in its own key colour and records `crop`), Luma Ray
  3.2 (an identical last frame becomes `loop: true`), MiniMax H3, Wan 3.0, Vidu Q3,
  LTX-2.5 (`camera_motion: static`; padded like Veo). Audio is turned off where the
  model has a switch.
- First and last frame canvas sizes must match; durations a model cannot render are
  refused here (`route_media.py` snaps them).
- **Every clip loses its audio locally** before it is published: the sound tracks are
  removed from the MP4 container (`media_mp4.py`; ffmpeg stream copy for a fragmented
  MP4), so the video samples stay byte-identical. The download itself is kept as
  `provider-download.mp4`, and `job.json` `audio` records what was removed.

The job id is written to `job.json` before the first poll, and the media is
downloaded the moment the job is done: result URLs expire (xAI "temporary", BytePlus
24 h, fal.ai about 7 days). `--timeout` defaults to 600 seconds for polling; each HTTP
operation is bounded. `--poll-interval` defaults to 5 seconds. A network error leaves
the job in place: `resume` polls and downloads it again without another paid request,
for xAI and Seedance video and every fal.ai request. Terminal `failed`/`expired` jobs
and media withheld after moderation are never replaced. Jobs written by the previous
version (`schemaVersion` 1) still resume.

## Outcomes: what each job status means

| `status` in job.json | Meaning | Next step |
| --- | --- | --- |
| `done` | Artifact published and hashed | Hand it to the owning skill for QA |
| `not_sent` | DNS failure, refused or failed connection, or a fal.ai input upload that failed: the paid request never left the machine | Fix the cause, then rerun with a new `--out-dir` |
| `failed` | The provider rejected the request (HTTP 4xx), refused it (a Gemini safety block), failed or expired the job, or withheld it after moderation | Read `error`; change the request; never resend it unchanged |
| `submit_unknown` (or `submitting` left by a crash) | The request may have reached the provider (timeout after sending, HTTP 5xx, unreadable response) | Check the provider's usage history, then settle the reservation (below) before any new request |
| `pending`, `pending_timeout`, `interrupted` (async jobs) | The provider job may still finish | `resume`; it only polls and downloads |
| `interrupted` with `partialArtifact` | Returned bytes could not be published; they are kept in the named `.partial` file | Recover the file; do not pay again |

Provider errors keep only whitelisted, scrubbed fields in `job.json` `error` and on
stderr: `httpStatus`, `code`, `type`, `param`, `message` (at most 300 characters,
with keys, URLs and long tokens removed) and `requestId`. Each adapter maps its
provider's errors onto `receipt.outcomeCode`: `ok`, `not_sent`, `submit_unknown`,
`auth`, `quota`, `rate_limit`, `moderation`, `entitlement`, `invalid_request`,
`provider_error`, `bad_response`, `partial_artifact`, `pending_timeout`, `failed`,
`expired` and others. Examples: Gemini's 400 `API_KEY_INVALID` and xAI's 400
"Incorrect API key" are `auth`; Gemini 402, BytePlus `AccountOverdueError` and a
fal.ai "Exhausted balance" are `quota`; BytePlus `*SensitiveContentDetected*`, fal.ai
`content_policy_violation` and OpenAI `moderation_blocked` are `moderation`.

## Spend ledger, opt-in caps and the duplicate guard

Every `--execute` first appends a `reserved` line to `<project>/.forge/ledger.jsonl`
(`--project-dir`, default: the current folder) and later commits the outcome:
`done`, `failed`, `unknown` or `not_sent`, with the provider's job id (`jobId`), the
artifact's `sha256` and, when the provider reports it (xAI video), the actual cost
(`actualUsd`). The file is append-only; the last line per `reservationId` wins. A
`reserved` or `unknown` reservation keeps holding its estimate, so a timeout can never
free budget that may have been spent.

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
relative to the jobs file (for `keyframe`, the path part of `PATH@SECONDS`):

```json
{"jobs": [
  {"id": "slime-idle", "command": "video", "provider": "xai", "model": "grok-imagine-video-1.5-lite", "prompt_file": "prompts/slime-idle.txt", "reference": ["art/slime.png"], "duration": 4, "out_dir": "jobs/slime-idle"},
  {"id": "crate", "command": "image", "provider": "gemini", "model": "gemini-3.1-flash-image", "prompt_file": "prompts/crate.txt", "size": "1024x1024", "out_dir": "jobs/crate"}
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
(also on `resume` for such a job). `--upload-url` sends xAI's `output.upload_url`
(the REST reference, checked 2026-10-06) for zero-data-retention teams. The upload
URL may be signed, so job.json stores only its host and hash.

## Outputs and verification

```text
<out>/prompt.txt       exact submitted prompt
<out>/job.json         schemaVersion 2: request plan, provider, model, estimate, fingerprint,
                       consent, capability (verifiedAt, docs), receipt (timing, attempt,
                       purpose, toolVersion, outcomeCode), ledger reservation, request and
                       job ids, error fields, output hash; video: audio, crop when padded
<out>/generated.png    or .jpg/.webp based on actual image bytes
<out>/generated.mp4    the clip without audio, still requiring decode/motion/alpha QA
<out>/provider-download.mp4   the clip as downloaded, when removing its audio changed it
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

API requests refuse redirects, so a credential is never forwarded. Credentials travel
as unredirected headers. Media downloads follow at most 3 HTTPS redirects, each one
rebuilt without any credential: the key goes only to a provider-hosted file on the
API host itself (a Gemini file URI), and never to the host it redirects to.

## Extending providers

Keep generation state separate from asset manifests. A new model of a supported
provider needs a capability record in [capabilities.json](capabilities.json) (with
`verifiedAt` and doc URLs, and for fal.ai a parameter map), a verified price row and
a mocked contract test (`tests/test_media_providers_*.py`). A new provider needs an
adapter class in `scripts/media_providers.py` (check, build, read_submit, status,
classify). Preserve user-selected models; do not silently fall back to a different
provider. Add a live result to the verification record only after a real call.
