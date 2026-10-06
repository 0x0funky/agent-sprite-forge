# Agent profiles for the local CLI routes

These files are agent profiles that `scripts/cli_media.py` hands to a local CLI. They are not
skills and carry their CLI's own frontmatter.

| Profile | Used by | Route |
|---|---|---|
| [video-agent.md](video-agent.md) | `cli_media.py video --route grok-acp` (also through `route_media.py video`) as `grok agent --agent-profile ... stdio` | Grok (local CLI) in ACP mode: the local video route |

The order of the media routes (see [route-media.md](../route-media.md)):

- images: the paid API of every provider whose key is configured (OpenAI, Google Gemini, xAI,
  BytePlus ModelArk, fal.ai, unless the user's `providers.order` says otherwise), then local: the
  host's own image tool, Codex (local CLI), then Grok (local CLI) in one-shot mode;
- video: the paid API when a key is configured (xAI, BytePlus ModelArk, fal.ai), then Grok
  (local CLI) in ACP mode with this profile, then a video the user already has;
- codeart2d only when the user asks for code-drawn art or no route exists.

An installed CLI is used as is; its first successful run records the VERIFIED proof for that CLI
version. A profile is part of its route's recipe (`grok-acp-video/1`): changing `video-agent.md`
means bumping that recipe id in `scripts/forge_doctor.py`, so the next run records a new proof.
