# Agent profiles for the local CLI routes

These files are agent profiles that `scripts/cli_media.py` hands to a local CLI. They are not
skills and carry their CLI's own frontmatter.

| Profile | Used by | Route |
|---|---|---|
| [video-agent.md](video-agent.md) | `cli_media.py video --route grok-acp` (or `--route auto`) as `grok agent --agent-profile ... stdio` | Grok (local CLI) in ACP mode: the first video route |

The order of the media routes is local agent first (see [cli-routes.md](../cli-routes.md)):

- images: the host's own image tool, then Codex (local CLI), then Grok (local CLI) in one-shot
  mode, then the paid REST API with the user's consent;
- video: Grok (local CLI) in ACP mode with this profile, then the paid REST API with the user's
  consent, then a video the user already has.

A local route is used only when `forge_doctor.py` reports it VERIFIED for the installed CLI
version, and only within the session cap. A profile is part of its route's recipe
(`grok-acp-video/1`): changing `video-agent.md` means bumping that recipe id in
`scripts/forge_doctor.py`, so the route is verified again.
