# Handoff: A4-media-safety

Branch `asf/A4-media-safety`, based on `wip/asf-upgrade-20261005` @ `cfed170`.
Module scope: generate2dmedia hardening (plan.md "## A4-media-safety", tasks A4-T1 to A4-T9).

Files changed: `skills/generate2dmedia/scripts/generate_media.py`, `tests/test_generate2dmedia.py`,
`skills/generate2dmedia/references/api-usage.md`, `skills/generate2dmedia/references/provider-survey.md`.
Files added: `skills/generate2dmedia/scripts/media_ledger.py`, `skills/generate2dmedia/references/prices.json`,
`tests/test_media_ledger.py`, this file.

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `skills/generate2dmedia` (`${CLAUDE_SKILL_DIR}` in Claude Code).
Every command is a dry run (no key, no network, no writes) until `--execute`.

```bash
python "<skill-dir>/scripts/generate_media.py" image --provider xai --model grok-imagine-image-2.0 --prompt-file prompts/crate.txt --resolution 1k --quality low --out-dir outputs/crate-api
python "<skill-dir>/scripts/generate_media.py" image --provider xai --model grok-imagine-image-2.0 --prompt-file prompts/crate.txt --resolution 1k --quality low --out-dir outputs/crate-api --execute --budget-usd 2 --max-calls 5 --purpose "crate prop v1"
python "<skill-dir>/scripts/generate_media.py" video --provider xai --model grok-imagine-video-1.5 --reference hero.png --prompt-file idle.txt --duration 4 --resolution 720p --out-dir outputs/hero-idle-api
python "<skill-dir>/scripts/generate_media.py" resume --job outputs/hero-idle-api/job.json
python "<skill-dir>/scripts/generate_media.py" batch jobs.json
python "<skill-dir>/scripts/generate_media.py" batch jobs.json --execute --workers 2 --budget-usd 5
python "<skill-dir>/scripts/media_ledger.py" summary
python "<skill-dir>/scripts/media_ledger.py" settle <reservation-id> --status failed
```

New flags on `image`/`video`: `--submit-timeout` (default 300, 1-600), `--project-dir` (default `.`;
ledger at `<project-dir>/.forge/ledger.jsonl`), `--budget-usd`, `--max-calls`, `--allow-duplicate`,
`--prices`, `--purpose`, `--base-url` + `--allow-custom-base-url`; `video` also `--upload-url`.
`resume` gains `--project-dir` and `--allow-custom-base-url`. New subcommand `batch` (`--workers 1|2`,
`--execute`, `--progress`, plus the batch-level safety flags). Environment cap: `FORGE_MAX_PAID_REQUESTS=N`
(`0` blocks every paid call). Output: one line of ASCII JSON on stdout; errors are one `error: ...` line on
stderr, exit 1 (130 when a single request is interrupted with Ctrl+C; a batch stops dispatching and
exits 1). Every entry point's `--help` works under `PYTHONIOENCODING=cp1252`.

## 2. SKILL.md routing rows

`skills/generate2dmedia/SKILL.md` (Z owns it). Frontmatter description, ready to paste (DOC-07, F-15):

```yaml
description: Generate or reference-edit game artwork through the OpenAI or xAI APIs, or animate an approved still through the xAI image-to-video API. Use only when the user explicitly asks for OpenAI/xAI API generation or has approved paid calls; dry-run first and show the consent list. Hand results to the sprite, video or map skill for game-ready processing.
```

Consent checklist, ready to paste (replaces "No need to ask again if already authorized"):

```markdown
## Consent checklist (every paid call)

1. Run the command without `--execute`. It needs no key, makes no network call and writes nothing.
2. Show the user the plan's `consent` block: provider, model, number of calls and the estimate in USD.
   If `estimateUsd` is null, say the price is unknown; never guess. Mention `apiHost` when a custom
   base URL is used, because the API key goes there.
3. Read `warnings`: an existing output folder, an identical earlier request, or a cap that would block.
4. Add `--execute` only after the user approves exactly that provider, model, call count and estimate.
   Pass the user's limits as `--budget-usd` / `--max-calls`. A host subscription is not API credit.
5. On `submit_unknown`, check the provider's usage history before any new request, then settle the
   reservation with `media_ledger.py settle`. Never resend an identical request without the user's
   go-ahead (`--allow-duplicate`).
```

Routing rows for the same SKILL.md:

| Need | Command |
| --- | --- |
| Plan one image or video and show its consent list | `generate_media.py image\|video ...` (no `--execute`) |
| Send one approved request | the same command plus `--execute` |
| Poll or download a video job without paying again | `generate_media.py resume --job <out>/job.json` |
| Many approved jobs with one consent list | `generate_media.py batch jobs.json`, then `--execute` |
| See spend, or settle an unknown outcome | `media_ledger.py summary` / `media_ledger.py settle <id> --status ...` |

`skills/generate2dmedia/agents/openai.yaml`: `policy: allow_implicit_invocation: false` (plan Z-T2).
`skills/generate2dsprite/SKILL.md:37` and `skills/video2dsprite/references/prompt-rules.md`: where they
say the commands are dry-run until `--execute`, add "and show the user the consent list first".

## 3. README tool-table rows

"Included Skills" row:

```markdown
| [`generate2dmedia`](./skills/generate2dmedia) | Explicitly requested OpenAI/xAI API image generation, reference edits and image-to-video | Raw media, job.json v2 receipts (request IDs, hashes, timing, estimate), a project spend ledger with caps and a duplicate guard |
```

Repository tree under `generate2dmedia/`:

```text
      scripts/generate_media.py     # image | video | resume | batch, dry-run by default
      scripts/media_ledger.py       # .forge/ledger.jsonl: caps, duplicate guard, settle
      references/api-usage.md
      references/provider-survey.md
      references/prices.json        # dated list prices with source and verifiedAt
```

Requirements line: unchanged (stdlib + Pillow); no new dependency.

## 4. CHANGELOG entries

Added
- generate2dmedia spend ledger `<project>/.forge/ledger.jsonl` (append-only reserve, then commit;
  statuses reserved, done, failed, unknown, not_sent; an unknown outcome keeps its reservation) and
  `media_ledger.py summary|settle`.
- Caps: `--budget-usd`, `--max-calls` and the `FORGE_MAX_PAID_REQUESTS` environment cap, checked before
  anything is sent, atomically across threads and processes.
- Dry-run consent list: `consent`, `estimate` (from `references/prices.json`, rows with source and
  verifiedAt), ledger totals and warnings.
- `batch jobs.json`: dry-run by default, at most 2 workers, progress file, reuses earlier successes,
  leaves earlier failures for a human, stops on account-level errors, never retries.
- job.json receipts: startedAt, submittedAt, completedAt, wallMs, providerMs, attempt, purpose,
  toolVersion, outcomeCode; plus clientRequestId / providerRequestId.
- `--submit-timeout`, `--purpose`, `--prices`, `--base-url` with `--allow-custom-base-url` (supersedes
  PR #16), `--upload-url` for xAI zero data retention (issue #11; field name unverified).

Changed
- stdout is one line of ASCII JSON instead of indented UTF-8 JSON; errors are `error: ...` lines.
- Provider errors now show whitelisted, scrubbed fields (code, type, param, message <= 300 chars,
  request id) instead of only the HTTP status.
- A video withheld after moderation is terminal (`failed`); returned image bytes that fail validation
  are kept as a `.partial` file instead of being dropped.

BREAKING
- An identical request that already succeeded (or is still open, or has an unknown outcome) is refused.
  Legacy switch: `--allow-duplicate`.
- job.json is `schemaVersion` 2 and the image submit timeout is 300 s (was 180 s). Legacy: v1 jobs still
  resume unchanged; `--submit-timeout 180` restores the old wait.

Fixed
- F-04: a paid image was deleted when the volume has no hard links (exFAT, FAT32, some network drives);
  publication now falls back to exclusive create + fsync and deletes the temp copy only after sha256
  verification, otherwise records `partialArtifact`.
- F-14: a paid success under a cp1252 console printed UnicodeEncodeError and exited 1.
- F-05 / issue #11: provider error reasons were hidden; DNS failures and refused connections were
  recorded as `submit_unknown` (now `not_sent`).
- F-16: the image submit timeout of 180 s was shorter than documented generation times.

## 5. Schema change requests

My documents follow plan Appendix B. They need these properties in A0's `media.schema.json`; if A0's
frozen `$defs` already allow them, nothing changes. `tests/test_generate2dmedia.py::
test_media_documents_validate_against_vendored_schema` validates a done image job, an interrupted (still pending) video job,
ledger lines and prices.json against the vendored schema; it skips until A0 is merged and must pass after.

`$defs/job_v2` (null while pending or unpriced; `artifact` absent until done):

```json
{
  "properties": {
    "estimate": {"properties": {"usd": {"type": ["number", "null"]}, "currency": {"type": "string"},
                                "items": {"type": "array", "items": {"type": "object"}}}},
    "receipt": {"properties": {
      "submittedAt": {"type": ["string", "null"]}, "completedAt": {"type": ["string", "null"]},
      "wallMs": {"type": ["integer", "null"]}, "providerMs": {"type": ["integer", "null"]},
      "attempt": {"type": "integer", "minimum": 1}, "purpose": {"type": ["string", "null"]},
      "outcomeCode": {"type": ["string", "null"]}}},
    "status": {"enum": ["submitting", "not_sent", "failed", "submit_unknown", "pending", "downloading",
                        "pending_timeout", "processing_result", "interrupted", "expired", "done"]},
    "route": {"const": "rest"},
    "apiBase": {"type": "string", "pattern": "^https://"},
    "consent": {"type": "object", "properties": {"provider": {"type": "string"}, "model": {"type": "string"},
                "calls": {"type": "integer"}, "estimateUsd": {"type": ["number", "null"]}, "apiHost": {"type": "string"}}},
    "ledger": {"type": "object", "properties": {"projectDir": {"type": "string"}, "reservationId": {"type": "string"},
               "status": {"enum": ["reserved", "done", "failed", "unknown", "not_sent"]}, "error": {"type": "string"}}},
    "clientRequestId": {"type": "string"}, "providerRequestId": {"type": "string"},
    "uploadUrl": {"type": "object", "properties": {"host": {"type": "string"}, "sha256": {"type": "string"}}},
    "partialArtifact": {"type": "object", "required": ["path", "sha256", "bytes"]},
    "error": {"type": "object", "properties": {"httpStatus": {"type": "integer"}, "code": {"type": "string"},
              "type": {"type": "string"}, "param": {"type": "string"}, "message": {"type": "string", "maxLength": 300},
              "requestId": {"type": "string"}}, "additionalProperties": false},
    "providerUsage": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}
  }
}
```

Do not list `artifact` in `job_v2.required`. `cliRun` is B22's and unused here.

`$defs/ledger_line_v1`: add `reservationId` (string; present on every line written here) and allow an
unpriced reservation:

```json
{"properties": {"reservationId": {"type": "string"}, "reservedUsd": {"type": ["number", "null"], "minimum": 0},
                "actualUsd": {"type": ["number", "null"], "minimum": 0}}}
```

`$defs/prices_v1`: optional top-level `schema`, `version` (used as `estimate.pricesVersion`), `currency`,
`notes`; optional row qualifiers `resolution`, `quality`, `size` (strings):

```json
{"properties": {"version": {"type": "string"}, "currency": {"const": "USD"}, "notes": {"type": "string"},
  "rows": {"items": {"properties": {"resolution": {"type": "string"}, "quality": {"type": "string"},
                                    "size": {"type": "string"}, "verifiedAt": {"type": "string", "format": "date"}}}}}}
```

New optional contract (add to media.schema.json if wanted): `$defs/batch_progress_v1`, the batch progress
file: `{schema: "batch_progress_v1", jobsFile, execution, workers, startedAt, updatedAt,
results[{id, status: generated|reused|left-for-human|failed, outcomeCode, jobDir, priorStatus?, artifact?,
estimateUsd?, error?}], inFlight[], remaining[], stopped, stopReason, complete}`.

## 6. Shared-helper promotion requests

forge_core is not vendored into generate2dmedia (A1-T9 targets sprite, map, video and codeart2d), so
generate2dmedia keeps stdlib twins:

- `media_ledger._local_utf8_stdio()` duplicates `forge_core.utf8_stdio()`; `generate_media.main()`
  calls it. Replace it if forge_core is ever vendored into generate2dmedia.
- `generate_media._write_new()` plus the publication block of `write_artifact()` (hard link, else
  exclusive create + fsync) is the same mechanism as `forge_core.publish_file_no_replace()`. If forge_core
  is vendored here, use it, but keep the sha256 verification and the `partialArtifact` record.
- `media_ledger.utc_timestamp()` (ISO 8601 UTC with milliseconds) is generic and could move to forge_core.

media_ledger API for B22 (Appendix A, frozen names unchanged): `load_prices(path=None)`,
`estimate(plan, prices=None)`, `fingerprint(plan, ref_hashes)`, `Ledger(project_dir)` with
`.reserve(entry)`, `.commit(reservation_id, *, actual_usd=None, status)`, `.find_success(fingerprint)`,
`.check_caps(budget_usd=None, max_calls=None)`. Added, all optional and keyword-only:
`reserve(..., budget_usd=None, max_calls=None, refuse_duplicate=False)` (caps and duplicate check run
under the ledger lock, so use these instead of a separate `check_caps` call when sending),
`check_caps(..., next_usd=0.0, quota_call=False, states=None)`; plus `Ledger.lines()`, `.entries()`,
`.totals()`, `.find(fingerprint, statuses, *, states=None)`, `.summary()`; exceptions `LedgerError`,
`CapExceeded`, `DuplicateRequest`; constants `LEDGER_API_VERSION = "1"`, `STATUSES`, `ROUTES`,
`QUOTA_ROUTES`, `MAX_PAID_ENV`; helpers `prices_version()`, `max_paid_requests()`, `utc_timestamp()`.
A reserve entry is `{jobDir, fingerprint, provider, model, kind, route, reservedUsd, quotaCall?}`;
`quotaCall` defaults to true for `codex-cli`, `grok-cli` and `grok-acp`, and `estimate()` returns 0 USD
for those routes. Quota calls count toward `max_calls` but not toward `FORGE_MAX_PAID_REQUESTS`.

## 7. Cross-module links that Z must add

- `skills/generate2dmedia/SKILL.md`: link `references/api-usage.md` (section "Consent before any paid
  call"), `references/prices.json` and `scripts/media_ledger.py`; add the consent checklist above.
- `skills/generate2dmedia/agents/openai.yaml`: `allow_implicit_invocation: false`.
- `.gitignore`: add `.forge/` (the ledger lives in user projects; this also protects the repo when a
  command runs at its root).
- README (all languages via the generator): the row and tree lines in section 3; the "API adapters are
  offline-contract-tested" sentence still holds.
- B22: `cli_media.py` should reserve/commit through `media_ledger.Ledger` with routes `codex-cli`,
  `grok-cli`, `grok-acp`; `forge_doctor.py` could report `Ledger(project).summary()["unsettled"]`.
- CHANGELOG: section 4 above; Appendix H rows "media | duplicate successful request refused |
  --allow-duplicate" and "media | job.json v2, image submit timeout 300 s | v1 jobs resumable" are covered.

## 8. Known limitations and what is not proven

- No live provider call was made. Every test uses fake transports, or the real transport with its
  network boundary replaced (socket creation, connect, `_open`). Provider error bodies in tests follow
  OpenAI's documented shape and xAI's commonly returned `{code, error}` shape; they are not captured
  from live failures. Outcome classification (auth, quota, rate_limit, moderation, entitlement) is
  keyword-based and therefore heuristic.
- `prices.json` rows were transcribed from `provider-survey.md` (provider pages checked 2026-10-05) and
  were not re-fetched; OpenAI image models have no verified row, so their estimates are null and
  `--budget-usd` refuses them. Only 720p video and two image-2.0 combinations are priced. A last frame
  is counted as one more input image, which is unverified.
- The xAI `upload_url` field name and how a zero-data-retention job reports completion are unverified.
  Whether providers log `X-Client-Request-Id` is unverified (the header is harmless if ignored).
- Provider terms links in `provider-survey.md` were added without fetching the pages.
- Caps are cumulative over the project's ledger; there is no time window.
- A crash between reserve and commit leaves a `reserved` line that keeps holding its estimate until it
  is settled. HTTP 5xx on submit is recorded as `unknown` (conservative).
- With two batch workers, a job already in flight still completes after a stop condition.
- The no-hard-link fallback is not atomic (exclusive create + fsync + sha256 check; a crash mid-write
  leaves the temp copy and no truncated final file is ever kept).
- Tests ran on Windows with Python 3.13 only. Sources parse with Python 3.10 grammar
  (`ast.parse(feature_version=(3, 10))`), but no 3.10 interpreter was run, and the POSIX `fcntl` lock
  branch of `media_ledger` has not been executed.
- The vendored-schema test skips until A0's `media.schema.json` is merged (see section 5).
