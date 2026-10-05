# Provider survey — checked 2026-10-05

This is an integration survey, **not a visual quality or latency benchmark**.
Official documentation was inspected; this update did not spend API credits.
Account access and future model availability require live verification.

## Implemented now

| Route | Relevant capability | Forge implementation |
| --- | --- | --- |
| Host image tool | Convenient interactive generation/editing | Existing agent workflow; inspect actual tool schema |
| OpenAI Images | Text generation and reference edits; configurable size/quality/background | `generate_media.py image --provider openai` |
| xAI Imagine Image | Generation and reference editing | `image --provider xai`; one reference per edit here |
| xAI Imagine Video | Image-to-video, asynchronous jobs | `video --provider xai` and GET-only `resume` |
| Any route above, in bulk | Many approved jobs with one consent list | `batch jobs.json`: dry-run first, at most 2 workers, no retries |
| Grok Build | Host's native video tool when exposed | Existing host route; no bundled daemon or login automation |

OpenAI's current documentation lists `gpt-image-2.5-sunburst` and
`gpt-image-2.5-flare`. The adapter requires a chosen model, uses Images rather than
Responses for single assets, and supports multipart reference edits. Do not infer
an API model ID from the name of the host's built-in image tool. Output capabilities
and organization access still depend on the selected model. [OpenAI image guide](https://developers.openai.com/api/docs/guides/image-generation),
[Sunburst model](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst).

xAI image editing is JSON rather than OpenAI's multipart schema. The current
`grok-imagine-image-2.0` accepts resolution and quality controls. Preserve actual
output dimensions in the asset manifest; asking for pixels in prose is not a
resolution guarantee. [Image generation](https://docs.x.ai/developers/model-capabilities/images/generation),
[editing](https://docs.x.ai/developers/model-capabilities/images/editing).

xAI videos use submit → request ID → poll → temporary output URL. The adapter
persists the ID before polling and never automatically repeats a paid submit.
`grok-imagine-video-1.5` supports image-to-video at up to 1080p; adding a last frame
uses reference-video mode, capped at 720p. Matching first/last stills is useful for
loop experiments, but motion velocity/pose continuity still needs inspection.
[Generation](https://docs.x.ai/developers/model-capabilities/video/generation),
[image-to-video](https://docs.x.ai/developers/model-capabilities/video/image-to-video),
[first/last frames](https://docs.x.ai/developers/model-capabilities/video/reference-to-video).

## Cost comparison for short experiments

USD list prices below are a dated snapshot, excluding tax and rejected/regenerated
candidates. A four-second example is an arithmetic illustration, not a quoted bill.
The adapter's estimates read the same rows from [prices.json](prices.json), where
each row records its `source` and `verifiedAt` date; a request without a matching
row is reported as unpriced rather than extrapolated.

| xAI route | 720p output / second | One input image | 4-second example | verifiedAt |
| --- | ---: | ---: | ---: | --- |
| `grok-imagine-video-1.5` | $0.14 | $0.01 | $0.57 | 2026-10-05 |
| `grok-imagine-video-1.5-lite` | $0.03 | $0.01 | $0.13 | 2026-10-05 |
| `grok-imagine-video` | $0.07 | $0.002 | $0.282 | 2026-10-05 |

Image-2.0 lists 1K low at $0.04/output, 2K medium at $0.08/output, plus $0.01 per
input image (verifiedAt 2026-10-05). Other resolution and quality combinations, and
480p or 1080p video, have no verified row yet. Last-frame/reference inputs may
change charges; the estimate counts a last frame as one more input image. OpenAI
image models have no verified row in this snapshot, so their estimates are null.
Re-check billing before batches; subscription quotas are not an API price model.
[xAI pricing](https://docs.x.ai/developers/pricing),
[Video 1.5 Lite capabilities](https://docs.x.ai/developers/models/grok-imagine-video-1.5-lite).

## Terms and output rights

Generated images and videos are governed by the terms of the provider that made
them, and so is what you may send as prompts and references. Read them before
shipping generated assets, and keep the provider and model recorded in `job.json`
as provenance. This survey is not legal advice.

- OpenAI: [Terms of Use](https://openai.com/policies/terms-of-use/),
  [Services Agreement](https://openai.com/policies/services-agreement/) (API
  customers), [Usage Policies](https://openai.com/policies/usage-policies/).
- xAI: [Terms of Service](https://x.ai/legal/terms-of-service),
  [Enterprise Terms](https://x.ai/legal/terms-of-service-enterprise) (API
  customers), [Acceptable Use Policy](https://x.ai/legal/acceptable-use-policy).

These terms links were added on 2026-10-05 without fetching the pages (offline
update); confirm they resolve, and which terms apply to your account, before relying
on them.

## Researched, not implemented in this release

| Candidate | Why evaluate it | Current limitation in Forge |
| --- | --- | --- |
| Google Veo 3.1 | Reference inputs and first/last-frame control | No adapter; model duration/resolution and person policies need their own validation |
| Google Gemini Omni video | Another current video-generation family | No adapter or quality comparison |
| Runway | Unified task API and several image/video models | No adapter; do not silently route to another model |

Veo 3.1 documents first/last-frame generation. Current 720p pricing lists Standard
at $0.40/s, Fast at $0.10/s, Lite at $0.05/s with audio. Model duration restrictions
mean these rates cannot always be compared using a four-second job.
[Veo guide](https://ai.google.dev/gemini-api/docs/veo),
[Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

Runway credits cost $0.01 each; Gen-4 Turbo lists 5 credits/s ($0.05/s).
This is a price comparison only, not evidence of equivalent game-animation quality.
[Runway pricing](https://docs.dev.runwayml.com/guides/pricing/).

## Recommended evaluation before a bigger batch

Use the same approved still for four cases: hero walk, monster attack, tree idle,
and water/fire background patch. Compare silhouette/identity, foot/root motion,
unwanted camera motion, loop endpoints, keying fringes and usable seconds. Record
cost **per accepted clip**, including rejected attempts; compare bytes and decoded
frame cost after identical packaging. More frames or a smaller MP4 alone does not
mean a better game asset.

Start with xAI for the existing workflow, using Lite as a cheaper candidate and
full 1.5 when endpoint control is valuable. That is an engineering recommendation
based on available controls and the project's previous workflow, not a measured
quality ranking. Keep sprite sheets for strict pixel animation and procedural
runtime particles for responsive combat effects.
