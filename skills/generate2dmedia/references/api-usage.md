# API adapter usage

Requires Python 3.10+ and Pillow. HTTP uses the standard library, with no provider
SDK dependency. Run from the repo root or resolve the script relative to this skill.
The output directory must not exist when executing a new request. Dry-run doesn't
create it. Secrets come from environment variables; this tool doesn't search the
filesystem for credentials or automatically load `.env` files.

## Images

```bash
python skills/generate2dmedia/scripts/generate_media.py image --provider openai --model gpt-image-2.5-sunburst --prompt-file hero.txt --size 1024x1024 --transparent --out-dir outputs/hero-api
python skills/generate2dmedia/scripts/generate_media.py image --provider openai --model gpt-image-2.5-sunburst --reference hero.png --prompt-file attack.txt --out-dir outputs/attack-api
python skills/generate2dmedia/scripts/generate_media.py image --provider xai --model grok-imagine-image-2.0 --reference forest.png --prompt-file forest-night.txt --resolution 2k --out-dir outputs/forest-api
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

## Videos and resume

```bash
python skills/generate2dmedia/scripts/generate_media.py video --provider xai --model grok-imagine-video-1.5 --reference hero.png --prompt-file idle.txt --duration 4 --resolution 720p --last-frame hero.png --out-dir outputs/idle-api --execute
python skills/generate2dmedia/scripts/generate_media.py resume --job outputs/idle-api/job.json --timeout 600
```

Models supported here: `grok-imagine-video` (480p/720p),
`grok-imagine-video-1.5` and `grok-imagine-video-1.5-lite` (also 1080p).
First/last pinning is implemented only for full 1.5 at 480p/720p because it switches
to reference-video mode. First/last canvas sizes must match. No video aspect override
is sent, avoiding stretched heroes. Full 1.5 requests silent output; other models'
audio can be stripped during packaging. No keyframes/edit/extend endpoints yet.

`--timeout` defaults to 600 seconds for polling; each HTTP operation is bounded.
Submission timeout is capped at 180 seconds. `--poll-interval` defaults to 5 seconds.
A network error leaves the job in place. Known video IDs can be polled again without
another paid generation. Terminal `failed`/`expired` jobs aren't automatically replaced.
An interrupted image response cannot be resumed through this adapter.

## Outputs and verification

```text
<out>/prompt.txt       exact submitted prompt
<out>/job.json         request plan, source hashes/size, job state, output hash
<out>/generated.png    or .jpg/.webp based on actual image bytes
<out>/generated.mp4    raw video, still requiring decode/motion/alpha QA
```

No API keys, base64 blobs, provider error bodies or signed media URLs are logged.
`returnedModel` can be null: a requested model is not evidence of the actual serving
revision. MP4 header validation only detects obvious non-video downloads; it is not
decode verification. Resume verifies the hash of already completed files.

The adapter refuses authenticated redirects and does not attach credentials to
media downloads. Downloads also reject redirects; if the provider starts returning
a redirecting media host, implement a bounded HTTPS-only redirect policy **without
forwarding authorization** and test it before enabling it.

## Extending providers

Keep generation state separate from asset manifests. Add a request builder, an
explicit capability gate, bounded status/download handling and mocked contract
tests. Preserve user-selected models; do not silently fall back to a different
provider. Add a live result to the verification record only after a real call.
