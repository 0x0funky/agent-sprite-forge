# B22-media-routes-doctor: forge_doctor readiness ladder, opt-in Codex/Grok CLI/ACP routes with provenance and adopt, guarded skill installer

Branch `asf/B22-media-routes-doctor`, based on `wip/asf-upgrade-20261005` @ `3f9252d`.
Plan tasks B22-T1 to B22-T5. Files added (no existing file changed):

- [skills/generate2dmedia/scripts/forge_doctor.py](../skills/generate2dmedia/scripts/forge_doctor.py): capability doctor (stdlib only, read-only) and the library `cli_media.py` uses for executable resolution, isolation env and proofs.
- [skills/generate2dmedia/scripts/cli_media.py](../skills/generate2dmedia/scripts/cli_media.py): the opt-in CLI routes, `resume --adopt`, `adopt --codex-thread`, `batch`.
- [skills/generate2dmedia/references/cli-routes.md](../skills/generate2dmedia/references/cli-routes.md): routes, consent, quota, provenance, error codes.
- [skills/generate2dmedia/references/agent-profiles/video-agent.md](../skills/generate2dmedia/references/agent-profiles/video-agent.md): the Grok ACP agent profile (Grok's own frontmatter format, not a SKILL.md).
- [tools/install_skills.py](../tools/install_skills.py): `--check` / `--apply` with manifest and backup.
- Tests: [tests/test_forge_doctor.py](../tests/test_forge_doctor.py), [tests/test_cli_media.py](../tests/test_cli_media.py), [tests/test_install_skills.py](../tests/test_install_skills.py); fake CLIs [tests/fixtures/fake_cli/fake_codex.py](../tests/fixtures/fake_cli/fake_codex.py) and [tests/fixtures/fake_cli/fake_grok.py](../tests/fixtures/fake_cli/fake_grok.py).

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `skills/generate2dmedia` (`${CLAUDE_SKILL_DIR}` in Claude Code).

    python "<skill-dir>/scripts/forge_doctor.py" --host-tools image_gen
    python "<skill-dir>/scripts/forge_doctor.py" --host-tools none --json --save outputs/doctor.json
    python "<skill-dir>/scripts/forge_doctor.py" --probe-auth
    python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli
    python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli --execute
    python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex
    python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex --execute --max-calls 5
    python "<skill-dir>/scripts/cli_media.py" image --route grok-cli --prompt-file prompts/forest.txt --output-dir outputs/forest-grok --execute
    python "<skill-dir>/scripts/cli_media.py" edit --route grok-cli --reference hero.png --prompt-file prompts/hero-night.txt --output-dir outputs/hero-night --execute
    python "<skill-dir>/scripts/cli_media.py" video --route grok-acp --reference hero.png --prompt-file prompts/idle.txt --duration 6 --resolution 720p --output-dir outputs/hero-idle-grok --execute
    python "<skill-dir>/scripts/cli_media.py" resume --run <run-id>
    python "<skill-dir>/scripts/cli_media.py" resume --run <run-id> --adopt
    python "<skill-dir>/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir outputs/hero-desktop
    python "<skill-dir>/scripts/cli_media.py" batch jobs.json
    python "<skill-dir>/scripts/cli_media.py" batch jobs.json --execute --max-calls 10

Repository tool (run from the checkout):

    python tools/install_skills.py --check --host claude
    python tools/install_skills.py --apply --host claude
    python tools/install_skills.py --apply --dest .claude/skills --skills generate2dsprite,generate2dmap --backup-dir backups/skills-old

- `forge_doctor.py`: text by default; `--json` prints the `doctor_v1` report as one line of ASCII JSON (`schema`, `tool`, `createdAt`, `overall`, `host`, `checks`, `cli`, `routes`, `proofs`, `elapsedMs`, plus `saved` when `--save` wrote a new file; `--save` refuses an existing file). Exit 1 when a check FAILs (the report still prints, and stderr says `error: N check(s) failed: ids`). `--no-exec` runs nothing at all; by default it runs only `ffmpeg -version` and `<cli> --version` of native executables, concurrently (Grok with its auto-updater off). Measured on the owner's machine with the real Codex 0.155.1 and Grok 1.0.40: 224 ms of doctor work, 0.45 s wall.
- `cli_media.py`: a dry run prints the plan (`consent`, `estimate`, `fingerprint`, `cli` presence/version/proof, exact `command`, `ledger` totals, `warnings`); `--execute` prints `{status, route, output, artifact, metadata, sha256, runRecord, version, verifiedBefore}`. Errors are one `error: CODE: message` line, ending in `(run <id>)` once a run record exists (use it with `resume --run`); exit 1, 130 on Ctrl+C. `resume`, `adopt` and `batch` print one JSON line (`batch` = the `batch_progress_v1` document plus `progressFile`).
- `install_skills.py`: `--check` (default) prints `missing: / changed (edited locally|outdated): / extra:` lines on stderr and exits 1 on drift, else `{"status": "in-sync", ...}`; `--apply` prints `{status, dest, skills, files, backup, manifest}`.
- Every `--help` (top level and each verb) is ASCII and exits 0 under `PYTHONIOENCODING=cp1252` and `cp950` (tested).

## 2. SKILL.md routing rows

`skills/generate2dmedia/SKILL.md`:

| Need | Route |
|---|---|
| Which art routes work here (once per session) | `scripts/forge_doctor.py --host-tools <your media tools or none>`; use only what `ROUTES` names |
| User asks for an image through their own Codex or Grok subscription | `scripts/cli_media.py image --route codex-cli\|grok-cli ...` dry run, show the consent block, then `--execute` |
| Reference edit through the Grok CLI | `scripts/cli_media.py edit --route grok-cli --reference <img> ...` |
| Animate an approved still through the Grok CLI (no API key) | `scripts/cli_media.py video --route grok-acp --reference <img> --duration 6 --resolution 720p ...` |
| A CLI run timed out or was interrupted | `scripts/cli_media.py resume --run <id>`, then `--adopt` (never reruns the CLI) |
| Codex Desktop made an image but it is not in the project | `scripts/cli_media.py adopt --codex-thread <thread-id> --output-dir <new>` |
| First use of a CLI route on this CLI version | `scripts/forge_doctor.py --verify-route <route>`, then `--execute` after consent |

Paste-ready section for **all five** SKILL.md files (Z note: "Capability check"), placed before the art-source decision:

```markdown
## Capability check (once per session)

Run `python "<skill-dir-of-generate2dmedia>/scripts/forge_doctor.py" --host-tools <tools>` where `<tools>`
lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`.
Use only the routes its ROUTES block names. An installed Codex or Grok CLI is not a connected tool:
the doctor offers a CLI route only after a verified run for that CLI version (see the generate2dmedia
CLI routes reference). Save the report next to your outputs with `--save <output>/doctor.json`.
If `encoding.stdout` FAILs, set `PYTHONUTF8=1` before running Python.
```

`skills/generate2dsprite/SKILL.md` (Codex host notes, replacing any "find the raw PNG under `$CODEX_HOME/generated_images`" step) and `skills/generate2dmap/SKILL.md`:

| Need | Route |
|---|---|
| Codex made the image in this session (Desktop or CLI) | `python "<generate2dmedia skill-dir>/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir <new>`; then process `generated.png` |

`skills/video2dsprite/SKILL.md`:

| Need | Route |
|---|---|
| Animate a still with the user's Grok subscription (opt-in) | generate2dmedia `cli_media.py video --route grok-acp`; then `video2dsprite.py process` on `generated.mp4` |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmedia/scripts/forge_doctor.py` | Reports which art routes work here (deps, ffmpeg, console encoding, skill drift, API keys, Codex/Grok CLI readiness ladder PRESENT -> AUTH_MODE -> TOOL_EXPOSED -> VERIFIED) without reading credentials or spending anything | `tests/test_forge_doctor.py` (cold machine, host tools, cp1252 FAIL, audit-hooked no-credential-read run, under 1 s) |
| `generate2dmedia/scripts/cli_media.py` | Opt-in image, edit and image-to-video through the user's own Codex or Grok CLI: dry-run consent, isolated child, kill on any other tool, quota ledger, provenance checks, resume and Codex Desktop adopt | `tests/test_cli_media.py` with fake CLIs |
| `tools/install_skills.py` | Installs the skills into Claude Code, Codex, Grok or any folder with a backup and a sha256 manifest; `--check` reports drift; never ships `__pycache__` | `tests/test_install_skills.py` |

Repository tree lines under `generate2dmedia/`:

```text
      scripts/forge_doctor.py       # capability check: readiness ladder, ROUTES, drift; reads no credentials
      scripts/cli_media.py          # opt-in Codex/Grok CLI routes, resume --adopt, adopt --codex-thread
      references/cli-routes.md      # routes, consent, quota, provenance, error codes
      references/agent-profiles/video-agent.md  # Grok ACP agent profile for image_to_video
```

and under the repository root: `tools/install_skills.py   # guarded install with backup + drift check`.

README install section (Claude Code, Codex, Grok copy installs; replaces `cp -R skills/* ...`, roadmap 7.2):

```bash
python tools/install_skills.py --apply --host claude
python tools/install_skills.py --check --host claude
```

## 4. CHANGELOG entries

Added
- `forge_doctor.py`: one capability check for every skill: Python and packages (from metadata, nothing imported), ffmpeg 5.1+ with libvpx/libx264, console and file encoding, sibling skills, vendored-copy agreement, install-manifest drift, project ledger, API keys (presence only), Codex/Grok CLI readiness ladder with version-keyed proofs, and a ROUTES block for image, image_edit, video, clip and code_art; `--host-tools`, `--json`, `--save`, `--no-exec`, `--probe-auth`, `--verify-route` (B22-T1; report v2 P1-2, 5.6).
- `cli_media.py`: opt-in routes `codex-cli` (image), `grok-cli` (image, edit) and `grok-acp` (image-to-video over ACP), dry-run by default, isolated environment, timeouts, kill on any unexpected tool call, typed error codes, quota calls in `.forge/ledger.jsonl` with `--max-calls` and the duplicate guard, `batch` with a progress file (B22-T2).
- Provenance: run records in `.forge/cli-runs/` written before the CLI starts, CLI ids saved as soon as reported, scoped output checks (own folder, no symlink or junction, fresh, one file, magic bytes, decode), exclusive copy with sha256, staged publication, `resume --adopt`, `adopt --codex-thread` (B22-T3; F-13).
- `tools/install_skills.py` with manifest and backup (B22-T4; F-11, roadmap 7.2).
- `references/cli-routes.md` (B22-T5).

Changed
- None (no existing file was modified).

BREAKING
- None.

Fixed
- F-13 / issue #4: an image made by Codex Desktop on Windows (not saved as a local PNG) can be adopted from the thread's `generated_images` folder or, failing that, from the thread's session rollout; supersedes draft PR #5 (`save_imagegen_result.py`, credit its author).
- F-11: a stale or hand-edited `~/.codex/skills` copy is detected (`install_skills.py --check`, and `forge_doctor.py` from the manifest).
- Roadmap 7.2: installs never copy `__pycache__` or bytecode.
- Report v2 3.4 / P1-2: an installed `grok.exe` is no longer mistaken for a usable video route.

## 5. Schema change requests

Against `shared/schemas/media.schema.json`. My tests validate every document against the vendored schema, and the proofs file against the schema with request 1 applied in memory (`tests/test_cli_media.py::ROUTE_PROOFS_V1`).

1. **New `$defs/route_proofs_v1`** (`<project>/.forge/route-proofs.json`; producer `cli_media.py`, consumer `forge_doctor.py`):

```json
"route_proofs_v1": {
  "description": "<project>/.forge/route-proofs.json written by cli_media.py: version-keyed evidence that an opt-in CLI route works. A record is valid only for its route, native tool, CLI version and recipe; forge_doctor.py reads it for the TOOL_EXPOSED and VERIFIED ladder steps.",
  "type": "object",
  "required": ["schema", "proofs"],
  "properties": {
    "schema": {"const": "generate2dmedia.route_proofs.v1"},
    "proofs": {"type": "array", "items": {
      "type": "object",
      "required": ["route", "tool", "version", "recipe", "level", "verifiedAt"],
      "properties": {
        "route": {"enum": ["codex-cli", "grok-cli", "grok-acp"]},
        "tool": {"enum": ["image_gen", "image_edit", "image_to_video"]},
        "version": {"type": "string", "minLength": 1},
        "versionText": {"type": "string", "minLength": 1},
        "recipe": {"type": "string", "pattern": "^[a-z0-9-]+/[0-9]+$"},
        "level": {"enum": ["TOOL_EXPOSED", "VERIFIED"]},
        "verifiedAt": {"$ref": "common.schema.json#/$defs/timestamp"},
        "runId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "method": {"type": "string"},
        "jobDir": {"$ref": "common.schema.json#/$defs/relPath"},
        "artifactSha256": {"$ref": "common.schema.json#/$defs/sha256"}
      },
      "if": {"properties": {"level": {"const": "VERIFIED"}}, "required": ["level"]},
      "then": {"required": ["artifactSha256"]}
    }}
  }
}
```

2. **Optional fields my producers add to `job_v2`** (they validate today because objects are open; please document them). Proposed fragment for `$defs/job_v2.properties`:

```json
"artSource": {"$ref": "common.schema.json#/$defs/artSource"},
"outputDir": {"$ref": "common.schema.json#/$defs/relPath"},
"adoptedAt": {"$ref": "common.schema.json#/$defs/timestamp"},
"adoption": {
  "description": "How the artifact was taken from the CLI's own output: method native-run-folder (a cli_media run), native-run-folder (resume --adopt), codex-thread-folder or codex-thread-rollout (adopt --codex-thread). source is symbolic (CODEX_HOME/... or GROK_HOME/sessions/*/...), never an absolute path.",
  "type": "object",
  "required": ["method", "source", "sourceSha256"],
  "properties": {
    "method": {"type": "string", "minLength": 1},
    "source": {"type": "string", "pattern": "^(CODEX_HOME|GROK_HOME)/"},
    "sourceSha256": {"$ref": "common.schema.json#/$defs/sha256"},
    "sourceMtime": {"type": ["string", "null"]},
    "freshAfter": {"$ref": "common.schema.json#/$defs/timestamp"},
    "threadId": {"type": "string"},
    "checks": {"type": "array", "items": {"type": "string"}},
    "finalAnswerNamesFile": {"type": "boolean"}
  }
},
"cliRun": {"properties": {
  "threadId": {"type": "string"}, "sessionId": {"type": "string"},
  "version": {"type": ["string", "null"]}, "versionText": {"type": ["string", "null"]},
  "recipe": {"type": "string", "pattern": "^[a-z0-9-]+/[0-9]+$"},
  "home": {"enum": ["CODEX_HOME", "GROK_HOME"]},
  "sourceRel": {"type": "string", "pattern": "^\\*/[0-9A-Za-z][0-9A-Za-z_-]{7,79}/(images|videos)/[0-9A-Za-z_.-]{1,120}$"},
  "verifiedBefore": {"type": "boolean"}
}},
"consent": {"properties": {"route": {"enum": ["rest", "codex-cli", "grok-cli", "grok-acp"]}, "quota": {"type": "boolean"},
                           "account": {"type": "string"}, "note": {"type": "string"}}}
```

   - `outputDir` appears only in run records (relative to `.forge/cli-runs/`); the published `job.json` omits it. `cliRun.cwd` is `<tmp>/forge-cli-<run>-<rand>` (the run folder's name, never an absolute path). For CLI routes `error` uses the existing closed object with `code` (the error code, e.g. `UNEXPECTED_TOOL`) and `message` (scrubbed, at most 300 characters); `receipt.outcomeCode` carries the same code, `ok`, or `adopted`.
   - `options` holds `{tool}` (plus `duration`, `resolution` for video; `adopted` for `adopt`); `requestedModel` is `<cli>-<tool>` (for example `codex-image_gen`, `grok-image_to_video`): the CLIs choose their own image model and report none.

3. **`doctor_v1` documentation** (no change needed; optional tightening). Extra top-level keys: `tool`, `createdAt`, `overall` (`OK|WARN|FAIL`), `host{declared, tools[], unrecognised[]}`, `cli{"<route>:<tool>": {route, tool, recipe, level (null|PRESENT|AUTH_MODE|TOOL_EXPOSED|VERIFIED), blocked, version, versionText, steps[{step, status, detail, remedy?}]}}`, `elapsedMs`, and `saved` (stdout only). `routes` keys: `image`, `image_edit`, `video`, `clip`, `code_art`, each `{route, status (ready|consent|unverified|limited|unknown|none), detail, options[]?}`; `route` is `host_image|host_image_edit|host_video|codex-cli|grok-cli|grok-acp|api|ffmpeg|png-frames|codeart2d|none`. `proofs{"<route>:<tool>": {level, version, recipe, verifiedAt, matchesInstalled}}`.

4. **Reused, unchanged**: `ledger_line_v1` (routes `codex-cli|grok-cli|grok-acp`, `quotaCall: true`, `reservedUsd: 0`), `batch_progress_v1` (`cli_media.py batch`, `workers` 1).

5. **Not proposed**: the install manifest `.agent-sprite-forge.install.json` (`schema: agent-sprite-forge.install.v1`, `tool`, `installedAt`, `source{commit, dirty}`, `skills[]`, `files[{path, sha256, bytes}]`, `backup`) is written by a repository tool into the user's skills folder and read by `forge_doctor.py`; add a `$def` only if Z wants it in the contracts.

## 6. Shared-helper promotion requests

generate2dmedia vendors no forge_core (and forge_core imports numpy, which the stdlib-only doctor must not need), so these stay local twins:

- `cli_media._local_staged_output(final)`, `_local_publish_directory_no_replace(stage, final)`, `_local_rename_noreplace(source, target)` (cli_media.py, "publication" section): stdlib twins of `forge_core.staged_output` / `publish_directory_no_replace` / `_rename_noreplace`, including the exclusive-mkdir fallback. Request: if forge_core's publishing block is ever split into a stdlib-only module (for example `shared/forge_publish.py`, vendored into all five skills), replace these with it. Tests: `test_qc_failure_publishes_nothing`, `test_refuses_an_existing_output_dir`.
- `forge_doctor._local_utf8_stdio()` and `forge_doctor.ascii_text()`: twins of `forge_core.utf8_stdio` / `ascii_text`; same condition.
- `forge_doctor.display_path(path)` (home shown as `~`, POSIX slashes) and `cli_media.mask_home(text)`: generic report helpers; candidates for forge_core if other skills start writing machine reports.
- `forge_doctor.isolated_env(cli)` and `forge_doctor.CREDENTIAL_ENV`: keep in generate2dmedia (only the CLI routes spawn third-party agents).

## 7. Cross-module links that Z must add

- generate2dmedia SKILL.md: link `references/cli-routes.md`, `scripts/forge_doctor.py`, `scripts/cli_media.py`; add the routing rows of section 2; say the CLI routes are opt-in and use the user's subscription quota. Keep the consent checklist from A4 and add "CLI routes: one quota call, not API credit".
- All five SKILL.md files: the "Capability check" section of section 2 (plain-text path to generate2dmedia's doctor; each skill links its own copy only if Z decides to vendor the doctor, which this module does not).
- generate2dsprite and generate2dmap SKILL.md (Codex notes): `adopt --codex-thread` instead of copying from `$CODEX_HOME/generated_images` by hand. README: "Codex Desktop adopt" (plan note).
- video2dsprite SKILL.md / prompt-rules: the opt-in `grok-acp` route as the subscription alternative to the xAI REST video route.
- README install section and CONTRIBUTING: `python tools/install_skills.py --apply|--check --host claude|codex|grok|agents` (backup + manifest; no `__pycache__`); developers keep `~/.codex/skills` in sync with `--check` (F-11).
- `.gitignore`: `.forge/` (also requested by A4) now also holds `cli-runs/` and `route-proofs.json`.
- Z-T5 `test_skill_packages`: exclude `references/agent-profiles/*.md` from any SKILL.md frontmatter allowlist (it is a Grok agent profile); `test_cli_encoding` should include `forge_doctor.py`, `cli_media.py` and `tools/install_skills.py`.
- Z-T8 evals: "never treat an installed Grok/Codex CLI as a connected tool", "no CLI route without consent", "never reuse a Grok login for REST".
- CHANGELOG: section 4; credit the PR #5 author for the adopt idea.

## 8. Known limitations and what is not proven

- **No live CLI run** (no quota spent; tests use fakes only). The recipes copy the owner's verified runs: Codex `exec` flags and prompt from `ai-native-game/v2/lib/codex-image.mjs` (verified on codex-cli 0.153.4), Grok headless image flags from `ai-native-game/lib/grok.mjs` (verified on Grok 1.0.5), the ACP video client from the Dusk `run-grok-video.mjs` run of 2026-09-13. The installed versions here are codex-cli 0.155.1 and Grok 1.0.40; their flags were confirmed only as strings inside the binaries. The first real run of each route on a new version is its verification, which is why proofs are keyed by CLI version and recipe id.
- Event shapes the adapters rely on (Codex `thread.started`/`item.*`, Grok `available_commands`/`tool_call`/`tool_call_update`/`end`, ACP `session/update`/`session/request_permission`) come from those logs and are mimicked by the fakes, not re-observed. Codex 0.153.4 printed no native image event, so Codex adoption trusts the thread's own `generated_images` folder, not an event receipt.
- Codex Desktop rollout parsing for `adopt --codex-thread` follows the record shapes documented by the sprite-gen survey (codex 0.140-0.149: `image_generation_call`, `image_generation_end`, `item_completed` Extension `image_gen.generation`); it was not run against a real Desktop rollout.
- Grok has no known read-only sign-in status command, so its AUTH_MODE step is UNKNOWN unless a proof implies it; `--probe-auth` reads only Codex's `login status` mode (never its output text).
- A cp1252 console is reported as FAIL (plan acceptance) even though every Forge CLI now reconfigures its own output; it protects the agent's own helper scripts.
- The doctor's ffmpeg check reads the build configuration (`-version`), not a functional encode (`forge_av.ffmpeg_info` does that).
- Windows only: tests ran on Windows 11 with Python 3.13; sources parse with the 3.10 grammar. The POSIX kill (`killpg`), `renameat2`/`renamex_np` publication and ELF/Mach-O executable checks were not executed. A file-symlink test skips without the Windows symlink privilege; the directory case runs with a junction.
- On Windows a killed CLI's process tree is ended with `taskkill /T /F` on its own PID only.
- Two `cli_media.py` processes writing proofs at the same moment can lose one record (last writer wins); the ledger itself is locked.
- `install_skills.py` copies every non-dot, non-bytecode file of each skill folder; it does not consult git, so untracked files inside a skill are installed too. `--check` compares exact bytes (a CRLF/LF difference counts as drift).
- Deviations from the plan text, with reasons:
  - `cli_media.py` uses `--output-dir` with staged publication (Appendix D) instead of generate_media's create-first `--out-dir`; the run id is persisted early in `.forge/cli-runs/<run>.json`, so a failed run leaves no output folder but stays adoptable.
  - Proofs live in the project (`.forge/route-proofs.json`), not beside the scripts as in the jev prototype: outputs never go into a skill folder.
  - `forge_doctor.py --verify-route` exists (the plan defers only running it live); it delegates to `cli_media.py` in-process.
  - The CLI child environment drops every credential-like variable and the calling agent's own session variables, not only the API keys: the fakes showed a host session token leaking otherwise.
  - The ACP agent profile is generic (duration and resolution come from the request), unlike the Dusk profile that hard-coded 6 s/720p.
