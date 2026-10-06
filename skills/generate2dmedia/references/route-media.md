# route_media.py: one command for every generated image or clip

`scripts/route_media.py` is the single entry point the Forge skills (and their pipeline scripts)
call for generated art. It picks the route, sends the request, and prints one JSON line. The
lower-level tools stay available: [`generate_media.py`](api-usage.md) for the paid APIs and
[`cli_media.py`](cli-routes.md) for the local CLIs.

## Route order (owner decision 2026-10-06)

1. **API**, every provider whose key is configured, in this order:
   - images: OpenAI (`gpt-image-2.5-sunburst`), Google Gemini (`gemini-3.1-flash-image`), xAI
     (`grok-imagine-image-2.0`), BytePlus ModelArk (`dola-seedream-5-0-pro-260628`), fal.ai
     (reference edits, `fal-ai/nano-banana-2/edit`);
   - video: xAI (`grok-imagine-video-1.5`), BytePlus ModelArk (Seedance
     `dreamina-seedance-2-0-260128`), fal.ai (`fal-ai/kling-video/v3/pro/image-to-video`).

   A configured key is the owner's consent: the request is sent at once, with no per-call question.
2. **Local**: the user's own signed-in CLI (subscription quota, never an API key). Images: Codex
   (`codex exec` with its native `image_gen`, reference images attached), then Grok one-shot
   (`image_gen`, or `image_edit` of one reference). Video: Grok in ACP mode (`image_to_video`).
   An installed CLI is used as is; its first successful run records the VERIFIED proof for that
   CLI version. An agent that has its own image tool (Codex `image_gen`, Grok's native tools) and
   no configured key may use that tool instead and adopt the result.
3. **No route**: `{"status":"no-route","fallback":"codeart2d"}` and exit 3. codeart2d draws code art
   only then, or when the user explicitly asks for code-drawn art.

The user's preference comes first: `"providers": {"order": [...]}` in the user config file (below)
lists providers (and, if wanted, local routes) to try before the rest, which keep this order.

`--route auto` (default) walks the order. `--route api` or `--route local` keeps one group;
`openai`, `gemini`, `xai`, `byteplus`, `fal`, `codex-cli`, `grok-cli` or `grok-acp` names one route,
and `fal:<endpoint-id>` one fal.ai model. Every API route is checked against its model's record in
[capabilities.json](capabilities.json) before anything runs. A route that cannot take the request
is skipped with the reason: too many references, a resolution it does not render, a first frame it
refuses, or a fal.ai image model with no reference to edit. A request is adapted, with a note, when:

- the model renders other lengths: the duration is snapped to the nearest one (the result says
  `durationRequested` and `durationUsed`). The API models' lengths come from
  [capabilities.json](capabilities.json); Grok (local CLI) renders 6 or 10 s, so 4 s becomes 6 s
  and 9 s becomes 10 s (a tie takes the longer length);
- the model cannot pin a last frame (the result says `lastFrameUsed: false`; Grok (local CLI)
  never pins one);
- the model takes no keyframes;
- the model has no native transparent background.

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
python "<skill-dir>/scripts/route_media.py" resolve --kind image --references 2 --tier hero
python "<skill-dir>/scripts/route_media.py" image --prompt-file prompts/master.txt --reference art/identity.png --reference art/peer.png --size 1024x1024 --out-dir outputs/hero-master
python "<skill-dir>/scripts/route_media.py" image --prompt-file prompts/master.txt --reference art/identity.png --route gemini --tier hero --out-dir outputs/hero-master-pro
python "<skill-dir>/scripts/route_media.py" video --prompt-file prompts/idle.txt --reference art/hero-master.png --last-frame art/hero-master.png --duration 6 --resolution 720p --out-dir outputs/hero-idle
python "<skill-dir>/scripts/route_media.py" video --prompt-file prompts/attack.txt --reference art/hero-master.png --last-frame art/hero-master.png --route fal:fal-ai/veo3.1/first-last-frame-to-video --provider-option fal:safety_tolerance="5" --out-dir outputs/hero-attack
python "<skill-dir>/scripts/route_media.py" video --prompt-file prompts/run.txt --reference art/hero-master.png --tier draft --out-dir outputs/hero-run --dry-run
```

| Option | Meaning |
|---|---|
| `--prompt-file` | UTF-8 prompt the agent wrote. Local routes take the size and every instruction from it |
| `--reference` | image: repeatable, in the order the prompt names them (OpenAI 16, Gemini 14, BytePlus 10, Codex 8, xAI 5, Grok one-shot 1; fal.ai image models need at least one); video: the first frame (the approved still) |
| `--size` | image: `WIDTHxHEIGHT` or `auto` (default `1024x1024`); each provider gets its own form (OpenAI `size`, Gemini aspect ratio and `1K`/`2K`/`4K`, xAI aspect ratio and `1k`/`2k`, Seedream `WxH`) |
| `--transparent` | image: a native transparent background where the model has one (OpenAI GPT Image); elsewhere a note says to key the backdrop |
| `--last-frame` | video: pins the end frame (same canvas as the first frame) where the route can; the result says `lastFrameUsed` |
| `--keyframe PATH@SECONDS` | video: an intermediate frame, up to 4 (xAI `grok-imagine-video-1.5` at 480p or 720p; other routes drop them with a note) |
| `--duration`, `--resolution` | video: 1..15 seconds (default 6, snapped per model; Grok (local CLI) 6 or 10); `480p`, `720p` (default) or `1080p` |
| `--model` | the API model (fal.ai: its endpoint id); with `auto`, only providers that list it are tried |
| `--tier` | `draft`, `standard` (default) or `hero`: each provider's model for that tier, for example Gemini Pro for hero masters and xAI 1.5 Lite for draft clips |
| `--provider-option [PROVIDER:]KEY=VALUE` | a request field passed as is (VALUE is JSON when it parses: `seed=7`, `safety_tolerance="5"`); `PROVIDER:` limits it to one provider; dotted keys nest; fields ASF sets itself are refused |
| `--out-dir` | a new folder: `generated.<ext>`, `job.json`, `prompt.txt`; an existing folder is refused |
| `--route` | `auto` (default), `api`, `local`, one route, or `fal:<endpoint-id>` |
| `--project-dir` | project root holding `.forge/` (ledger, run records, proofs); default the current folder |
| `--dry-run` | print the plan and estimate of the route that would run; no key is used, nothing is sent or written |
| `--budget-usd`, `--max-calls`, `--timeout`, `--purpose` | opt-in caps, the route's time limit, receipt text |

## Output

Success, exit 0 (one ASCII JSON line; the first five keys are the contract):

```json
{"status":"ok","route":"api:openai","artifact":"outputs/hero-master/generated.png","sha256":"<64 hex>","estimateUsd":null,"kind":"image","label":"OpenAI API (gpt-image-2.5-sunburst)","model":"gpt-image-2.5-sunburst","job":"outputs/hero-master/job.json"}
```

- `route` is `api:openai`, `api:gemini`, `api:xai`, `api:byteplus`, `api:fal`, `local:codex-cli`,
  `local:grok-cli` or `local:grok-acp`.
- `estimateUsd` is the list-price estimate from [prices.json](prices.json), `null` when no verified
  price row exists, `0.0` for a local route (subscription quota).
- Video adds `lastFrameUsed`, `durationRequested` and `durationUsed` (the length sent), and
  `duration` when the length was snapped. `crop` appears when the frames were padded to 16:9 for
  a model without 1:1 (fal.ai Veo 3.1 and LTX): crop every frame
  to that box (fractions of the frame) to return to the still's aspect. `notes` explains every
  adaptation. `attempts` lists earlier routes that refused the request (`route`, `code`,
  `message`, `keptIn`).
- Every clip is published without audio (the download is kept as `provider-download.mp4`).
- `resolve` prints `{"status":"ok","route":...,"order":[...],"available":[...],"skipped":[...]}`
  (video adds `pinsLastFrame`); `--dry-run` prints `"status":"dry-run"` with the estimate.

No route, exit 3: `{"status":"no-route","fallback":"codeart2d","kind":...,"skipped":[...]}`. A
failure prints one `error: <route>: <code>: <message>` line and exits 1 (a model or option that
cannot be sent is also an error, never a no-route); a usage error exits 2; Ctrl+C exits 130.

## Keys and the user config file

Each provider reads only its own key, the vendor's official variable name:

| Provider | Key variable | Get a key |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | platform.openai.com (GPT Image may need API organization verification) |
| Google Gemini | `GOOGLE_API_KEY` or `GEMINI_API_KEY` (`GOOGLE_API_KEY` wins when both are set) | aistudio.google.com (a paid-tier key; image models have no free tier) |
| xAI | `XAI_API_KEY` | console.x.ai |
| BytePlus ModelArk | `ARK_API_KEY` | console.byteplus.com, ModelArk; activate the Seedream and Seedance models first |
| fal.ai | `FAL_KEY` (`key-id:key-secret`) | fal.ai/dashboard/keys |

A key comes from the environment, or from one JSON file in the user's own configuration folder,
never inside a project; the environment wins over the file:

| System | File |
|---|---|
| Windows | `%APPDATA%\agent-sprite-forge\config.json` |
| macOS, Linux | `$XDG_CONFIG_HOME/agent-sprite-forge/config.json`, default `~/.config/agent-sprite-forge/config.json` |

```json
{
  "OPENAI_API_KEY": "sk-...",
  "GEMINI_API_KEY": "...",
  "XAI_API_KEY": "xai-...",
  "ARK_API_KEY": "...",
  "FAL_KEY": "key-id:key-secret",
  "models": {"gemini-image-hero": "gemini-3-pro-image", "xai-video-draft": "grok-imagine-video-1.5-lite", "fal-video": "luma/agent/ray/v3.2/image-to-video"},
  "providers": {"order": {"image": ["gemini", "openai"], "video": ["byteplus"]}}
}
```

Every field is optional. `models` slots are `<provider>-<kind>` (the standard tier) and
`<provider>-<kind>-draft` / `-hero`; `providers.order` is one list for both kinds or one per kind.
On macOS and Linux keep the file private (`chmod 600`; the doctor warns otherwise). Keys are read
in-process only: they are never printed, logged, written, passed on a command line or given to a
child process, and a Codex or Grok CLI sign-in is never used as an API key. A provider never falls
back to another provider's key. `forge_doctor.py` reports each provider as configured yes or no
(`apiKeys`, `providers`), never a key, and shows the resolved order (`routeOrder`).

## What each provider supports

| Provider | Images | Video | Pins the last frame | Notes |
|---|---|---|---|---|
| OpenAI | generate and edit, 16 references, native alpha | none | n/a | sizes in multiples of 16 |
| Google Gemini | generate and edit, 14 references | none (Veo through Gemini is not added; its previews end 2026-10-22) | n/a | SynthID watermark; Pro for hero masters |
| xAI | generate and edit, 5 references | 1 to 15 s, 480p to 1080p | yes, 1.5 at 480p or 720p; up to 4 keyframes | silent generation on 1.5; Lite is the draft model |
| BytePlus ModelArk | Seedream generate and edit, 10 references | Seedance 4 to 15 s | yes, any resolution (identical frames allowed) | first frame 300 to 6000 px; no real faces |
| fal.ai | reference edits only | Kling, Veo 3.1, Luma, MiniMax, Wan, Vidu, LTX | yes on every curated model | inputs go to the fal CDN first; Veo and LTX are padded to 16:9 |

The full survey, with the shutdown dates, is [provider-survey.md](provider-survey.md).

## Spend records, no caps by default

Every call, paid or local, is a line in `<project>/.forge/ledger.jsonl` with its estimate; an API
call's final line also names the provider's job id (`jobId`) and the artifact's `sha256`, and xAI
video records the actual cost the provider reported (`actualUsd`). Each output folder keeps
`job.json` (route, provider, model, job id, prompt hash, reference hashes, estimate, receipt,
artifact hash). There is no cap unless one is asked for: `--budget-usd`, `--max-calls`,
`FORGE_MAX_PAID_REQUESTS`, and for the local routes `FORGE_SESSION_IMAGES` /
`FORGE_SESSION_VIDEOS` (window `FORGE_SESSION_HOURS`, default 12). An identical earlier request is
not refused: a new `--out-dir` is a new take, and `receipt.attempt` counts the takes.
`python "<skill-dir>/scripts/media_ledger.py" summary` shows the totals.

## Test seam

When `FORGE_ROUTE_MEDIA_FAKE` names a script, `route_media.py` parses and checks the command as
usual (files exist, `--out-dir` is new, values in range) and then runs `python <script> <the same
arguments>`, returning the script's output and exit code unchanged. Tests of the skills that call
`route_media.py` use it to fake generation: the fake writes `generated.png` or `generated.mp4`
into `--out-dir` and prints the success line, or prints the no-route line and exits 3.
