# Provider survey: last verified 2026-10-06

This survey is about integration, **not** a benchmark of quality or latency. Each request shape was
checked against the vendor's official documentation on 2026-10-06. No API call was made: the owner
has no keys, so the community reports real-world differences. Machine-readable limits live in
[capabilities.json](capabilities.json) (one record per model, with `verifiedAt` and doc URLs) and
list prices in [prices.json](prices.json).

## Shutdowns and changes

| Date | What | What ASF does |
|---|---|---|
| 2026-05-12 | OpenAI `dall-e-2` and `dall-e-3` removed | Refused |
| 2026-06-25 | Gemini `gemini-3.1-flash-image-preview` and `gemini-3-pro-image-preview` shut down | Refused; the stable ids work |
| 2026-08-17 | Imagen 4 (`imagen-4.0-*`) shut down | Refused |
| 2026-09-24 | Sora 2 API shut down | Never supported |
| 2026-09-30 | Bedrock Nova Canvas and Nova Reel ended (Titan on 06-30) | Never supported |
| 2026-10-02 | Gemini `gemini-2.5-flash-image` shut down | Refused |
| **2026-10-22** | Veo 3.1 preview ids on the Gemini API shut down; the successor `gemini-omni-1.1-flash` has no 1:1 and no audio-off | Veo is **not** added through the Gemini API; use fal.ai's Veo 3.1 |
| **2026-10-23** | OpenAI `gpt-image-1` ends | Dropped now (refused); the default is `gpt-image-2.5-sunburst` |
| 2026-10-26 | LTX v1 ends | ASF sends LTX-2.5 through fal.ai |
| 2026-11-02 | xAI `grok-imagine-image-quality` retired (then served by `grok-imagine-image-2.0` at quality low) | Works with a warning until then |
| 2026-12-01 | OpenAI `gpt-image-1.5`, `gpt-image-1-mini`, `chatgpt-image-latest` end | Work with a warning until then |

Runway's own gen4.x models cannot pin a last frame, and Midjourney still has no official API.

## Supported in ASF

| Provider | Key | Images | Video (image to video) | How it answers |
|---|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `gpt-image-2.5-sunburst` (default), `gpt-image-2.5-flare`, `gpt-image-2`; until 12-01 `gpt-image-1.5`, `gpt-image-1-mini` | none | Synchronous base64 |
| Google Gemini | `GOOGLE_API_KEY` or `GEMINI_API_KEY` (GOOGLE wins) | `gemini-3.1-flash-image` (default), `gemini-3-pro-image` (hero), `gemini-3.1-flash-lite-image` (draft) | none (no Veo here) | Synchronous inline base64 |
| xAI | `XAI_API_KEY` | `grok-imagine-image-2.0` (default), `grok-imagine-image` | `grok-imagine-video-1.5` (default), `grok-imagine-video-1.5-lite` (draft), `grok-imagine-video` | Images synchronous; video job, then poll |
| BytePlus ModelArk | `ARK_API_KEY` | `dola-seedream-5-0-pro-260628` (default), `dola-seedream-5-0-flash-260915` (draft) | `dreamina-seedance-2-0-260128` (default), `-mini-260615` (draft), `-fast-260128`, `dreamina-seedance-2-5-260628` (hero) | Images synchronous; video task, then poll |
| fal.ai | `FAL_KEY` | reference edits: `fal-ai/nano-banana-2/edit` (default), `fal-ai/nano-banana-pro/edit` (hero), `bytedance/seedream/v5/pro/edit`, `openai/gpt-image-2/edit` | `fal-ai/kling-video/v3/pro/image-to-video` (default), `.../v3/standard/...`, `fal-ai/veo3.1/first-last-frame-to-video` and `.../fast/...`, `luma/agent/ray/v3.2/image-to-video`, `minimax/h3/image-to-video`, `alibaba/wan-3.0/image-to-video` (draft), `fal-ai/vidu/q3/image-to-video`, `lightricks/ltx-2.5/image-to-video/pro` and `/fast` | Queue: submit, status, result |

What each provider can do for a sprite pipeline:

| Provider | References | Native alpha | Last frame | Keyframes | Audio off | 1:1 |
|---|---|---|---|---|---|---|
| OpenAI | 16 (multipart `image[]`) | **yes** (`background: transparent`, PNG) | n/a | n/a | n/a | yes |
| Gemini | 14 inline parts (Flash: 10 objects + 4 characters; Pro: 6 + 5 + 3 style) | no | n/a | n/a | n/a | yes (`imageConfig.aspectRatio`) |
| xAI | images 5 on 2.0 (`images`), 1 on classic | no | `last_frame` on 1.5 at 480p or 720p | up to 4 (`keyframes` with `timestamp_s`), 1.5 only | `generate_audio: false` on 1.5 | follows the still |
| BytePlus | Seedream 10 | only from one RGBA input | Seedance `role: last_frame` (identical frames allowed) | no | `generate_audio: false` | `ratio: "1:1"` (2.0); 2.5 is `adaptive` only |
| fal.ai | Nano Banana 14, Seedream 10, GPT Image 2 16 | GPT Image 2 edit only | every curated video model | no | per model | per model; Veo and LTX are 16:9 only (padded) |

Every clip is stripped of audio locally after download, whatever was requested
(`media_mp4.py`: the sound tracks are removed from the MP4 container, so the video samples stay
byte-identical; the download is kept as `provider-download.mp4`).

### Request details worth knowing

- **OpenAI**: generations are JSON, edits multipart with repeated `image[]` (a JSON `images`
  array also exists). `response_format` is legacy and never sent. For GPT Image 2 and 2.5, width
  and height are multiples of 16, the aspect lies between 1:3 and 3:1, the image has 655,360 to
  8,294,400 pixels and no edge above 3840. Quality is `low`, `medium`, `high`, `auto`, plus `xhigh`
  and `max` on 2.5. A moderation block is a 400 with `code: moderation_blocked`.
- **Gemini**: `POST /v1beta/models/{model}:generateContent` with the key in `x-goog-api-key`; one
  user turn with the text part, then one `inlineData` part per reference, in order;
  `generationConfig.responseModalities: ["IMAGE"]` and `imageConfig {aspectRatio, imageSize}`
  (`imageSize` is uppercase: `512`, `1K`, `2K`, `4K`; Pro has no 512, Lite only 1K). The model
  thinks first and may return interim images marked `thought: true`; ASF keeps the last
  non-thought image. A bad key is a **400** with reason `API_KEY_INVALID`; 402 means prepaid credit
  is used up. A safety block is `promptFeedback.blockReason` or a `finishReason` such as
  `IMAGE_SAFETY`. All output carries SynthID. A file result (`fileData.fileUri`) is downloaded
  with the key on the API host only; any redirect is followed without it.
- **xAI**: video `POST /v1/videos/generations`, then `GET /v1/videos/{request_id}` until
  `status` is `done` (HTTP 202 while pending). The temporary video URL is downloaded at once.
  `aspect_ratio` is never sent for image-to-video (it would stretch the still). A reference-pinned
  clip (last frame or keyframes) is capped at 720p. A moderated clip comes back with
  `respect_moderation: false` and no URL. `usage.cost_in_usd_ticks` (10^10 ticks per USD) is
  recorded as the actual cost. Image edits are JSON, never multipart. The zero-data-retention
  upload URL goes in `output.upload_url`.
- **BytePlus ModelArk**: base `https://ark.ap-southeast.bytepluses.com/api/v3`. Seedream watermarks
  by default (ASF sends `watermark: false`), answers JPEG unless asked (`output_format: png`) and
  renders 921,600 to 4,624,220 pixels (1024x1024 is fine; a smaller size is raised). Seedance takes
  first frames of 300 to 6000 px with an aspect of 0.4 to 2.5, 4 to 15 s (2.5: 30 s), refuses
  real human faces in its inputs, and cannot mix first/last frames with reference images. Task
  statuses: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `expired`; the video URL lives
  24 h. Errors are `{"error": {"code"}}` codes such as `AuthenticationError`, `AccountOverdueError`,
  `RateLimitExceeded.*`, `*SensitiveContentDetected*`, `ModelNotOpen`.
- **fal.ai**: `Authorization: Key $FAL_KEY`. Submit `POST https://queue.fal.run/{endpoint}`, then
  poll the returned `status_url` (`IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`), then GET the returned
  `response_url`. The URLs are stored and never rebuilt (the documented and SDK shapes differ), and
  the key goes only to the queue host. A failure is `COMPLETED` with `error` and `error_type`, or a
  4xx result. Inputs are uploaded to the fal CDN first, as the official client does (free
  requests, made before the paid one); `--provider-option fal_upload=data` sends data URIs instead.
  Results stay at least 7 days. Kling's end frame is `end_image_url` on fal.ai (Kling's own API
  calls it `image_tail`); MiniMax uses `768P` for 720p and ASF turns its prompt expansion off;
  Vidu Q3's start-end endpoint is gone, so the end frame goes to its image-to-video endpoint.

## The wider market (not adapters)

### Image APIs

| Vendor | Ids | Refs | Alpha | Price per image | Note |
|---|---|---|---|---|---|
| BFL | flux-3-image, flux-2-pro/max/flex | 8 (FLUX.2), 10 (FLUX 3) | no | FLUX 3 $0.048 at 1K | Async; URL lives 10 min (FLUX 3: 1 h) |
| Stability | stable-image ultra/core, sd3.5-large | 1 | background removal | $0.025-0.08 | Pixel-art preset |
| Ideogram | ideogram-4-5, ideogram-4-transparent, ideogram-3-character | 5, or 1 character + 10 style | **native** | $0.03-0.10 (sec.) | Attribution required |
| Recraft | recraftv4_1, recraftv3 | 10 (style only) | background removal | $0.007-0.04 | Vector and pixel styles |
| Leonardo | lucid-origin, phoenix-v1.0 | 6 | some | $0.0125-0.05 (sec.) | Resells other models |
| Adobe Firefly | image4_standard, image4_ultra, Image5 | style and structure only | no | by contract | No identity references |
| Alibaba | qwen-image-3.0, qwen-image-2.1-pro, wan2.7-image | 3 to 10 | yes (2.1-pro) | $0.03-0.075 | Trial output is non-commercial |
| Tencent | hy-image-v3, hy-image-v3.5-preview | 3; 20 | no | $0.024-0.032 | Use through an aggregator |

Also: Bria FIBO (licensed training data, indemnity), Reve (1 to 6 references), PixelLab (4- or
8-direction characters) and Retro Diffusion (pixel sprites from about $0.01). "sec." marks a
secondary source.

### Video APIs

| Vendor | Last frame | 1:1 / audio off | Price | Note |
|---|---|---|---|---|
| Veo 3.1 (Vertex, fal.ai) | `lastFrame` | no / Vertex only | $0.20/s on fal.ai at 720p, audio off | Top realism; 4, 6 or 8 s |
| Gemini Omni (`gemini-omni-1.1-flash`) | `<LAST_FRAME>` | no / no | about $0.10/s | Weak control |
| Runway gen4.5 | hosted models only | 960:960 / silent | $0.12/s | Its own models cannot pin the end |
| Luma ray-3.2 | `end_image_url`; `loop` | yes / no audio | $0.15 per 5 s at 540p | Best loop control |
| Kling v3 | `image_tail` (fal.ai: `end_image_url`) | follows input / sound off | $0.084-0.112/s | Top motion quality |
| MiniMax H3 | `end_image_url` | follows input / no switch | $0.05/s at 480P | Strong body motion |
| Wan 3.0 | `end_image_url` | yes / yes | $0.10/s at 720p | Preview |
| Vidu Q3 | `end_image_url` | follows input / `audio: false` | $0.154/s at 720p | May cut between shots |
| PixVerse v6 | transition endpoint | yes / off by default | about $0.04/s | Turn multi-clip off |
| LTX-2.5 | `end_image_url` | no / yes | $0.12/s at 720p (pro) | `camera_motion: static` |
| FLUX 3 video | keyframe 2 | yes / yes | $0.17/s | Preview |
| Pika 2.5 | 2 to 5 keyframes | ? | $0.04/s | Official API now exists |

### Aggregators

| Aggregator | Coverage | Shape | Output lives |
|---|---|---|---|
| **fal.ai** (adapter) | nearly every model above, plus Gemini, GPT Image, FLUX and Seedream images | queue: submit, status, result | at least 7 days |
| Replicate | Veo 3.1, Kling v3, Seedance 2.0, Wan, Vidu, Luma; Nano Banana 2, FLUX.2, Seedream 5 | predictions, `Prefer: wait` | **1 h** |
| OpenRouter | GPT Image, Gemini, FLUX, Seedream; Veo 3.1, Seedance, Kling v3, Wan, H3 | `/api/v1/videos`, normalized `frame_images` | ? |
| Together | FLUX, Gemini, GPT Image 2, Seedream; Veo 3.1, Seedance, H3, Vidu, Wan | `frame_images` | "download immediately" |
| WaveSpeed, Segmind, Runware | subsets of the above | task APIs | 7 days and other |

Avoid resellers in the style of Kie.ai and PiAPI (some sell unofficial Midjourney access).

## Next wave and skips

- **Next**: OpenRouter (a normalized schema, but its video API is new), Kling direct (JWT
  signing; prepaid packs expire), BFL, Alibaba DashScope.
- **Only through an aggregator**: Veo (Vertex needs OAuth), Luma, MiniMax, Wan, Vidu, PixVerse,
  LTX, Pika, Hunyuan, Ideogram, Recraft, Leonardo, Runway.
- **Skip**: Sora, Midjourney, Imagen, Bedrock image, Adobe, Runway gen4.x for end-pinned clips.

## Pitfalls

- **URL expiry**: BFL 10 min; Replicate and Luma 1 h; Tencent 12 h; BytePlus, DashScope, Vidu,
  Recraft 24 h; Runway 24-48 h; Gemini files 2 days; fal.ai and WaveSpeed 7 days; xAI
  "temporary". ASF saves the job id first and downloads the moment a job is done.
- **Content filters**: BFL "Content Moderated", an empty Ideogram URL, PixVerse status 7, fal.ai
  `content_policy_violation`, Seedance `*SensitiveContentDetected*`, xAI `respect_moderation:
  false`. Veo image-to-video takes adult people only. Weapons or gore in attack clips may need
  OpenAI `moderation: low` or fal.ai Veo `safety_tolerance` (pass them with `--provider-option`).
  ASF never retries a blocked job.
- **Aspect**: Veo, Gemini Omni and LTX have no 1:1: ASF pads the still onto a 16:9 canvas in the
  still's own key colour and records the crop (`crop` in job.json and in route_media's result),
  so later steps can crop back. Vidu needs similar start and end aspects; ASF requires the same
  canvas. Seedream 4.5 and 5-lite cannot render 1024x1024 (5.0 pro and flash can).
- **Minimum durations**: Veo 4 s, LTX 6 s, Luma 5 s (10 s only with keyframes), Seedance 4 s,
  Kling 3 s, MiniMax 5 s. route_media.py snaps a duration to the nearest allowed value and says so.
- **Audio is on by default** on xAI, Seedance 2.x, Vidu, LTX, Wan, MiniMax, Veo and Gemini Omni.
  ASF turns it off where a switch is documented and strips it locally anyway.
- **Watermarks**: SynthID on every Google image; Seedream watermarks by default (ASF turns it off);
  HappyHorse adds visible text; Ideogram and Runway require attribution.
- **Multi-shot output**: Vidu Q3 may cut; turn off PixVerse multi-clip and Kling `multi_prompt`.
- **Retries**: never retry a paid POST (ASF never does). PixVerse needs a fresh `Ai-trace-id` per
  request.

## Prices

[prices.json](prices.json) holds the list prices ASF estimates with (verified 2026-10-06), for
example: OpenAI GPT Image 2.5 at 1024x1024 about $0.013 (medium) and $0.053 (high), output only;
Gemini 3.1 Flash Image $0.067 at 1K, Pro $0.134; xAI image-2.0 $0.04 to $0.08 plus $0.01 per input,
video 1.5 $0.08 / $0.14 / $0.25 per second at 480p / 720p / 1080p, Lite $0.02 / $0.03 / $0.14;
Seedream 5.0 pro $0.045, flash $0.018; Seedance 2.0 $0.07 / $0.15 / $0.37 per second (16:9;
square clips cost less), mini $0.04 / $0.08; fal.ai Kling v3 pro $0.112/s, Veo 3.1 $0.20/s,
Wan 3.0 $0.10/s at 720p. A request without a matching row is reported as unpriced; nothing is
extrapolated. Re-check billing before a batch.

## Terms and output rights

Generated images and videos are governed by the terms of the provider that made them, and so is
what you may send as prompts and references. Read them before shipping generated assets, and keep
the provider and model recorded in `job.json` as provenance. This survey is not legal advice.

- OpenAI: [Terms of Use](https://openai.com/policies/terms-of-use/),
  [Services Agreement](https://openai.com/policies/services-agreement/),
  [Usage Policies](https://openai.com/policies/usage-policies/).
- xAI: [Terms of Service](https://x.ai/legal/terms-of-service),
  [Enterprise Terms](https://x.ai/legal/terms-of-service-enterprise),
  [Acceptable Use Policy](https://x.ai/legal/acceptable-use-policy).
- Google, BytePlus and fal.ai: read the API terms on each provider's site; fal.ai's terms also
  cover the hosted models it resells, whose own licences may add conditions.

## Recommended evaluation before a bigger batch

Use the same approved still for four cases: hero walk, monster attack, tree idle, and a water or
fire background patch. Compare silhouette and identity, foot and root motion, unwanted camera
motion, loop endpoints, keying fringes and usable seconds. Record cost **per accepted clip**,
including rejected attempts, and compare bytes and decoded frame cost after identical packaging.
More frames or a smaller MP4 alone does not mean a better game asset.

## Sources

OpenAI [image guide](https://developers.openai.com/api/docs/guides/image-generation),
[Images reference](https://developers.openai.com/api/reference/resources/images),
[deprecations](https://developers.openai.com/api/docs/deprecations);
Google [image generation](https://ai.google.dev/gemini-api/docs/generate-content/image-generation),
[generateContent](https://ai.google.dev/api/generate-content),
[API keys](https://ai.google.dev/gemini-api/docs/api-key),
[errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors),
[pricing](https://ai.google.dev/gemini-api/docs/pricing),
[deprecations](https://ai.google.dev/gemini-api/docs/deprecations);
xAI [video generation](https://docs.x.ai/developers/model-capabilities/video/generation),
[reference to video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video),
[multi-image editing](https://docs.x.ai/developers/model-capabilities/images/multi-image-editing),
[pricing](https://docs.x.ai/developers/pricing);
BytePlus [image API](https://docs.byteplus.com/en/docs/ModelArk/1541523),
[video task](https://docs.byteplus.com/en/docs/ModelArk/1520757),
[query task](https://docs.byteplus.com/en/docs/ModelArk/1521309),
[error codes](https://docs.byteplus.com/en/docs/ModelArk/1299023),
[pricing](https://docs.byteplus.com/en/docs/ModelArk/1544106);
fal.ai [queue](https://fal.ai/docs/model-apis/model-endpoints/queue),
[errors](https://fal.ai/docs/documentation/model-apis/errors.md),
[media expiration](https://fal.ai/docs/documentation/model-apis/media-expiration),
[fal_client uploads](https://github.com/fal-ai/fal/blob/main/projects/fal_client/src/fal_client/client.py)
and each model's page `https://fal.ai/models/<endpoint-id>`.
