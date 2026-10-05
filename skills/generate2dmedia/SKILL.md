---
name: generate2dmedia
description: Generate or reference-edit game artwork through OpenAI or xAI APIs, or animate an approved still through the xAI image-to-video API. Use when an API workflow is requested or the host lacks the needed media tool; hand the result to the sprite, video, or map processor for game-ready exports.
---

# Generate 2D Media

This optional generation adapter complements the host agent's media tools. It does
not replace the planning/QA in `generate2dsprite`, `video2dsprite` or `generate2dmap`.
Use available host tools when they fit the request. Respect tool and user routing
requirements; installing this skill does not authorize bypassing a host's tool policy.

## Choose the route

- **Original image or reference edit:** OpenAI Images API or xAI Imagine Image.
- **Animate approved character/prop/scene still:** xAI Imagine Video API.
- **Existing video:** skip generation; use [video2dsprite](../video2dsprite/SKILL.md).
- **Grok Build native media tool:** use its actual exposed schema. It is an optional
  host route, not a prerequisite for other agents and not interchangeable with REST.

Read [API usage](references/api-usage.md) for flags, recovery and outputs. Read
[provider survey](references/provider-survey.md) when choosing providers or models;
it separates implemented adapters from researched candidates. Model names and prices
are dated evidence, not permanent defaults. The CLI requires an explicit model.

## Execution contract

1. Plan the art/animation with the owning skill. Inspect an existing reference before
   describing it. Write the prompt yourself into a UTF-8 file; the script doesn't
   invent art direction. Keep references on disk until execution ends.
2. Run the proposed command without `--execute` to validate its local inputs and
   inspect the request plan. This makes no network calls and does not require a key.
3. When paid generation is within the user's authorized scope, add `--execute`.
   Use `OPENAI_API_KEY` / `XAI_API_KEY` from the environment; never embed keys in
   prompts, browser builds, output manifests or committed files. A host subscription
   does not establish API credit availability. No need to ask again if already authorized.
4. One execution creates one image or one video job in a **new** output directory.
   No automatic paid POST retries or hidden provider/model fallback. A video job ID
   is saved immediately; `resume` performs GET/download only. If submission outcome
   is unknown and no ID was received, inspect provider history before starting anew.
5. Treat the result as **raw generated media**. Validate identity, real dimensions,
   camera/anchor drift, transparency, motion and loop seams with the owning skill.
   Provider success is not game-ready approval. Keep exact prompts, input hashes,
   job ID and result hash for reproducibility; report provider/model as requested
   and separately as returned when supplied.

Example (from repository root; replace paths with the installed skill location):

```bash
python skills/generate2dmedia/scripts/generate_media.py video --provider xai --model grok-imagine-video-1.5 --reference hero.png --prompt-file idle.txt --duration 4 --resolution 720p --out-dir outputs/hero-idle-api
```

Add `--execute` only when ready. Same first/last image can constrain the endpoints
with `--last-frame hero.png` at 480p/720p, but it does not guarantee a natural cycle.
Ordinary image-to-video leaves the image aspect ratio unchanged.

This version has offline contract tests and local processing tests. A provider is
only **live-tested** after an actual authorized call succeeds; do not claim quality,
latency, account availability or a live backend version from dry-run tests.
