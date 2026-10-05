# B22-media-routes-doctor: forge_doctor readiness ladder, opt-in Codex/Grok CLI/ACP routes with provenance and adopt, guarded skill installer

Branch `asf/B22-media-routes-doctor`, based on `wip/asf-upgrade-20261005` @ `3f9252d`.
Plan tasks B22-T1 to B22-T5. Files added (no existing file changed):

> **Integration (2026-10-05, group pass "runtime-media", branch `asf/int-g-runtime-media`).** Owner decision 13 is
> implemented (D22): local agent first in the doctor's ROUTES, user-facing "Codex (local CLI)" / "Grok (local CLI)"
> names, `cli_media.py --route auto`, and a default session cap of 8 images and 2 videos per 12 hours enforced
> through the ledger. Re-verification after a CLI update works (D23), the drift check ignores bytecode and OS junk
> (D24), batch progress files hold relative paths (D25), and D26-D29 apply. The schema requests of section 5 were
> applied by the shared stage. Open items for Z are marked **Z**.

- [skills/generate2dmedia/scripts/forge_doctor.py](../skills/generate2dmedia/scripts/forge_doctor.py): capability doctor (stdlib only, read-only) and the library `cli_media.py` uses for executable resolution, isolation env and proofs.
- [skills/generate2dmedia/scripts/cli_media.py](../skills/generate2dmedia/scripts/cli_media.py): the local CLI routes (Codex / Grok (local CLI), `--route auto`, session cap), `resume --adopt`, `adopt --codex-thread`, `batch`.
- [skills/generate2dmedia/references/cli-routes.md](../skills/generate2dmedia/references/cli-routes.md): routes, consent, quota, provenance, error codes.
- [skills/generate2dmedia/references/agent-profiles/video-agent.md](../skills/generate2dmedia/references/agent-profiles/video-agent.md): the Grok ACP agent profile (Grok's own frontmatter format, not a SKILL.md), and [agent-profiles/README.md](../skills/generate2dmedia/references/agent-profiles/README.md) (integration): which route uses it and the route order.
- [tools/install_skills.py](../tools/install_skills.py): `--check` / `--apply` with manifest and backup.
- Tests: [tests/test_forge_doctor.py](../tests/test_forge_doctor.py), [tests/test_cli_media.py](../tests/test_cli_media.py), [tests/test_install_skills.py](../tests/test_install_skills.py); fake CLIs [tests/fixtures/fake_cli/fake_codex.py](../tests/fixtures/fake_cli/fake_codex.py) and [tests/fixtures/fake_cli/fake_grok.py](../tests/fixtures/fake_cli/fake_grok.py).

## 1. CLIs

Run from the user's project root; `<skill-dir>` is `skills/generate2dmedia` (`${CLAUDE_SKILL_DIR}` in Claude Code).

    python "<skill-dir>/scripts/forge_doctor.py" --host-tools image_gen
    python "<skill-dir>/scripts/forge_doctor.py" --host-tools none --json --save outputs/doctor.json
    python "<skill-dir>/scripts/forge_doctor.py" --probe-auth
    python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli
    python "<skill-dir>/scripts/forge_doctor.py" --verify-route codex-cli --execute
    python "<skill-dir>/scripts/cli_media.py" image --route auto --prompt-file prompts/hero.txt --output-dir outputs/hero-local
    python "<skill-dir>/scripts/cli_media.py" image --route auto --prompt-file prompts/hero.txt --output-dir outputs/hero-local --execute
    python "<skill-dir>/scripts/cli_media.py" video --route auto --reference hero.png --prompt-file prompts/idle.txt --output-dir outputs/hero-idle-local --execute
    python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex
    python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-codex --execute --max-calls 5
    python "<skill-dir>/scripts/cli_media.py" image --route codex-cli --prompt-file prompts/hero.txt --output-dir outputs/hero-more --execute --session-images 12
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
- `cli_media.py`: a dry run prints the plan (`route`, `label`, `consent`, `estimate`, `fingerprint`, `cli` presence/version/proof, exact `command`, `ledger` totals with `session` usage, `warnings`, and `routeChoice` for `--route auto`); `--execute` prints `{status, route, label, output, artifact, metadata, sha256, runRecord, version, verifiedBefore, routeChoice?}`. Errors are one `error: CODE: message` line, ending in `(run <id>)` once a run record exists (use it with `resume --run`); exit 1, 130 on Ctrl+C; argparse usage errors exit 2 (D26); anything unexpected is `error: internal error (<Type>: <message>)`, scrubbed (D27). `resume`, `adopt` and `batch` print one JSON line (`batch` = the `batch_progress_v1` document plus `progressFile`; its `jobsFile` and `jobDir` are relative to the progress file, D25).
- `--route auto` (D22) takes the first local route of the owner's order (images: `codex-cli`, then `grok-cli`; edits: `grok-cli`; video: `grok-acp`) that is VERIFIED for the installed CLI version and names it in `routeChoice` (route, label, order, skipped routes and why); none VERIFIED gives `NOT_VERIFIED` and runs nothing. Batch jobs may say `"route": "auto"`.
- Session cap (D22): every executed local-route call is checked in `media_ledger.reserve` under the ledger lock against at most 8 images (image and edit) and 2 videos within the last 12 hours of the project's ledger. `--session-images N`, `--session-videos N`, `--session-hours H` (flags win) or `FORGE_SESSION_IMAGES`, `FORGE_SESSION_VIDEOS`, `FORGE_SESSION_HOURS` change it; `0` blocks a kind; a refused call is `CAP` and runs nothing. Dry runs, batch plans, `media_ledger.py summary` and the doctor's `media.ledger` check show the usage.
- `forge_doctor.py --verify-route ROUTE --execute` passes `--allow-duplicate` and the hidden `--verification` flag, which puts the installed CLI version into `options.verifies` and so into the fingerprint (D23): verifying again after a CLI update is a new request and is never refused as a duplicate.
- `install_skills.py`: `--check` (default) prints `missing: / changed (edited locally|outdated): / extra:` lines on stderr and exits 1 on drift, else `{"status": "in-sync", ...}`; `--apply` prints `{status, dest, skills, files, backup, manifest}`.
- Every `--help` (top level and each verb) is ASCII and exits 0 under `PYTHONIOENCODING=cp1252` and `cp950` (tested).

## 2. SKILL.md routing rows

`skills/generate2dmedia/SKILL.md`:

| Need | Route |
|---|---|
| Which art routes work here (once per session) | `scripts/forge_doctor.py --host-tools <your media tools or none>`; use only what `ROUTES` names, local agent first |
| An image with no host image tool, a local route VERIFIED (`ROUTES` status `ready`) | `scripts/cli_media.py image --route auto ... --execute`: no per-call question within the session cap; tell the user which route ran ("Codex (local CLI)" or "Grok (local CLI)") |
| User asks for an image through their own Codex or Grok subscription | `scripts/cli_media.py image --route codex-cli\|grok-cli ...` dry run, show the consent block, then `--execute` |
| Reference edit through the Grok CLI | `scripts/cli_media.py edit --route grok-cli --reference <img> ...` |
| Animate an approved still through the Grok CLI (no API key) | `scripts/cli_media.py video --route grok-acp --reference <img> --duration 6 --resolution 720p ...` (or `--route auto`); the first video route when VERIFIED |
| The session cap stopped a local route (`CAP`) | ask the user: raise it (`--session-images N`, `FORGE_SESSION_IMAGES`), wait, or take the paid REST route with their consent |
| A CLI run timed out or was interrupted | `scripts/cli_media.py resume --run <id>`, then `--adopt` (never reruns the CLI) |
| Codex Desktop made an image but it is not in the project | `scripts/cli_media.py adopt --codex-thread <thread-id> --output-dir <new>` |
| First use of a CLI route on this CLI version, or after a CLI update | `scripts/forge_doctor.py --verify-route <route>`, then `--execute` after consent (Grok `image_edit` is verified by one consented `cli_media.py edit ... --execute`) |

Paste-ready section for **all five** SKILL.md files (Z note: "Capability check"), placed before the art-source decision:

```markdown
## Capability check (once per session)

Run `python "<skill-dir-of-generate2dmedia>/scripts/forge_doctor.py" --host-tools <tools>` where `<tools>`
lists the media tools in your own tool list (`image_gen`, `image_edit`, `image_to_video`) or `none`.
Use only the routes its ROUTES block names; they come local agent first. `ready` means use it now: your
own tool, or Codex / Grok (local CLI) VERIFIED for the installed version, which runs without a per-call
question within the session cap (8 images and 2 videos per 12 hours); always name the route you used.
`consent` (the paid API) needs the user's consent for each request. An installed Codex or Grok CLI is not
a connected tool: a CLI route is offered only after a verified run for that CLI version (see the
generate2dmedia CLI routes reference). Save the report next to your outputs with `--save <output>/doctor.json`.
If `encoding.stdout` FAILs, set `PYTHONUTF8=1` before running Python.
```

`skills/generate2dsprite/SKILL.md` (Codex host notes, replacing any "find the raw PNG under `$CODEX_HOME/generated_images`" step) and `skills/generate2dmap/SKILL.md`:

| Need | Route |
|---|---|
| Codex made the image in this session (Desktop or CLI) | `python "<generate2dmedia skill-dir>/scripts/cli_media.py" adopt --codex-thread <thread-id> --output-dir <new>`; then process `generated.png` |

`skills/video2dsprite/SKILL.md`:

| Need | Route |
|---|---|
| Animate a still with the user's Grok subscription: Grok (local CLI), the first video route when VERIFIED | generate2dmedia `cli_media.py video --route auto` (Grok ACP mode); then `video2dsprite.py process` on `generated.mp4`; without a VERIFIED route, the xAI REST route needs the user's consent |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `generate2dmedia/scripts/forge_doctor.py` | Reports which art routes work here, local agent first (deps, ffmpeg, console encoding, skill drift, API keys, ledger and session cap, Codex/Grok CLI readiness ladder PRESENT -> AUTH_MODE -> TOOL_EXPOSED -> VERIFIED) without reading credentials or spending anything | `tests/test_forge_doctor.py` (cold machine, host tools, route order, cp1252 FAIL, audit-hooked no-credential-read run, re-verification across a CLI update, under 1 s) |
| `generate2dmedia/scripts/cli_media.py` | Image, edit and image-to-video through the user's own Codex or Grok CLI (Codex / Grok (local CLI)): `--route auto` to the first VERIFIED route, session cap, dry-run plan, isolated child, kill on any other tool, quota ledger, provenance checks, resume and Codex Desktop adopt | `tests/test_cli_media.py` with fake CLIs |
| `tools/install_skills.py` | Installs the skills into Claude Code, Codex, Grok or any folder with a backup and a sha256 manifest; `--check` reports drift, ignoring bytecode, dot files and OS junk; never ships `__pycache__` | `tests/test_install_skills.py` |

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
- Integration (D22): `cli_media.py --route auto` (the first VERIFIED local route, named in `routeChoice`) and the local routes' session cap (8 images, 2 videos per 12 hours; `--session-images/--session-videos/--session-hours`, `FORGE_SESSION_*`), enforced through the ledger; `references/agent-profiles/README.md`.

Changed
- Integration (D22): ROUTES puts the local agent first and marks a VERIFIED local route `ready` (no per-call question within the session cap) instead of `consent`; user-facing route names are "Codex (local CLI)" and "Grok (local CLI)" (one-shot mode for images, ACP mode for video).
- Integration (D25): `cli_media.py batch` progress files store `jobsFile` and `jobDir` relative to the progress file.
- Integration (D29): the doctor's `tool.version` and the receipts' `toolVersion` (`cli_media/0.4.0`) name the package version.

BREAKING
- None.

Fixed
- F-13 / issue #4: an image made by Codex Desktop on Windows (not saved as a local PNG) can be adopted from the thread's `generated_images` folder or, failing that, from the thread's session rollout; supersedes draft PR #5 (`save_imagegen_result.py`, credit its author).
- F-11: a stale or hand-edited `~/.codex/skills` copy is detected (`install_skills.py --check`, and `forge_doctor.py` from the manifest).
- Roadmap 7.2: installs never copy `__pycache__` or bytecode.
- Report v2 3.4 / P1-2: an installed `grok.exe` is no longer mistaken for a usable video route.
- Integration (D23): `--verify-route ROUTE --execute` after a Codex or Grok update was refused as a DUPLICATE, so the doctor's own remedy failed; two verifications within one second also collided on their output folder.
- Integration (D24): `install_skills.py --check` and the doctor's `skills.install` reported the bytecode that running an installed skill writes as drift.
- Integration (reviewer notes): error messages turned relative paths of 40+ characters into `[blob]`; the `grok-cli:image_edit` remedy offered `--verify-route grok-cli`, which verifies `image_gen` only.

## 5. Schema change requests

Resolved by the shared stage (shared-stage commit f3d7eb2): `$defs/route_proofs_v1` (its description now cites D23), the
documentation of the `job_v2` fields cli_media writes (`artSource`, `outputDir`, `adoptedAt`, `adoption`, the extra
`cliRun` and `consent` fields) and of the `doctor_v1` keys (including the D22 route order), and `batch_progress_v1`
(described as relative per D25). The tests validate against the real `media.schema.json` (`assert_valid_contract`); the
in-memory `ROUTE_PROOFS_V1` copy is gone.

New optional fields from the integration pass (objects are open, so everything validates today; document them):

- `job_v2.routeChoice`: `{requested: "auto", route, label, order[], skipped[], version | null, verifiedFor?, note?}`, present
  when `--route auto` chose the route.
- `job_v2.options.verifies`: the installed CLI version a `--verify-route` run verifies (D23; part of the fingerprint).
- `doctor_v1.routes.<need>.options[].label`: the user-facing route name; a VERIFIED local route has status `ready`.
- `media_ledger.py summary` gains `session {image, video, since, hours, caps{image, video}}` (not a contract document).

Follow-up for the schema owner: `batch_progress_v1.jobsFile` and `results[].jobDir` can now be typed as `relPath`
(both batch tools write relative paths, D25); files written before D25 held absolute paths.

## 6. Shared-helper promotion requests

generate2dmedia vendors no forge_core (and forge_core imports numpy, which the stdlib-only doctor must not need), so these stay local twins:

- `cli_media._local_staged_output(final)`, `_local_publish_directory_no_replace(stage, final)`, `_local_rename_noreplace(source, target)` (cli_media.py, "publication" section): stdlib twins of `forge_core.staged_output` / `publish_directory_no_replace` / `_rename_noreplace`, including the exclusive-mkdir fallback. Request: if forge_core's publishing block is ever split into a stdlib-only module (for example `shared/forge_publish.py`, vendored into all five skills), replace these with it. Tests: `test_qc_failure_publishes_nothing`, `test_refuses_an_existing_output_dir`.
- `forge_doctor._local_utf8_stdio()` and `forge_doctor.ascii_text()`: twins of `forge_core.utf8_stdio` / `ascii_text`; same condition.
- `forge_doctor.display_path(path)` (home shown as `~`, POSIX slashes) and `cli_media.mask_home(text)`: generic report helpers; candidates for forge_core if other skills start writing machine reports.
- `forge_doctor.isolated_env(cli)` and `forge_doctor.CREDENTIAL_ENV`: keep in generate2dmedia (only the CLI routes spawn third-party agents).

## 7. Cross-module links that Z must add

- **Z** generate2dmedia SKILL.md: link `references/cli-routes.md`, `scripts/forge_doctor.py`, `scripts/cli_media.py`; add the routing rows of section 2. Policy text (owner decision 13, amended decision 10, D22): local agent first; a local route is used only when VERIFIED; a VERIFIED route runs without a per-call question within the session cap and the route used is always named; an unverified route needs consent for its one verification call; REST needs consent for every paid request; never raise the session cap or pass `--allow-duplicate`/`--allow-unverified` unless the user asks. Keep the consent checklist from A4 and add "local CLI routes: one quota call each, not API credit". The narrowed frontmatter description should cover the local routes too, not only "explicit OpenAI/xAI API generation".
- All five SKILL.md files: the "Capability check" section of section 2 (plain-text path to generate2dmedia's doctor; each skill links its own copy only if Z decides to vendor the doctor, which this module does not).
- generate2dsprite and generate2dmap SKILL.md (Codex notes): `adopt --codex-thread` instead of copying from `$CODEX_HOME/generated_images` by hand. README: "Codex Desktop adopt" (plan note).
- video2dsprite SKILL.md / prompt-rules: Grok (local CLI) in ACP mode (`grok-acp`, or `cli_media.py video --route auto`) is the first video route when VERIFIED, ahead of the xAI REST video route (consent).
- README install section and CONTRIBUTING: `python tools/install_skills.py --apply|--check --host claude|codex|grok|agents` (backup + manifest; no `__pycache__`); developers keep `~/.codex/skills` in sync with `--check` (F-11).
- `.gitignore`: `.forge/` (also requested by A4) now also holds `cli-runs/`, `route-checks/` and `route-proofs.json`.
- Owner decision 14 live validation: the per-project session cap (8 images, 2 videos per 12 h) is lower than the validation's caps (Codex 10 images, Grok 4 images, 4 videos); set `FORGE_SESSION_IMAGES` / `FORGE_SESSION_VIDEOS` for that run, or use one project folder per task.
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
- The session cap is a rolling window over the project's ledger (default 12 hours), not a per-conversation count: the ledger knows no agent session, and a window cannot be reset by rerunning a tool. Separate project folders have separate caps. The doctor's ROUTES does not look at the session usage (its `media.ledger` check WARNs once a cap is reached; `cli_media.py` refuses the call).
- `--route auto` in a dry run cannot read a Grok version (or a Codex outside npm) without starting the CLI, so it plans with the newest VERIFIED proof (`verifiedFor`); `--execute` reads the version and checks again.
- The Codex event handling stays an allowlist (reviewer note): an unknown item type stops the run as `UNEXPECTED_TOOL` and names the type; the first live run of a new Codex version shows whether a benign new type appears.
- `routes.code_art` reports `ready` from `skills/codeart2d/scripts` while codeart2d has no SKILL.md yet (`skills.present` warns); this resolves when Z adds it.
- `install_skills.py` copies every non-dot, non-bytecode file of each skill folder; it does not consult git, so untracked files inside a skill are installed too. `--check` compares exact bytes (a CRLF/LF difference counts as drift).
- Deviations from the plan text, with reasons:
  - `cli_media.py` uses `--output-dir` with staged publication (Appendix D) instead of generate_media's create-first `--out-dir`; the run id is persisted early in `.forge/cli-runs/<run>.json`, so a failed run leaves no output folder but stays adoptable.
  - Proofs live in the project (`.forge/route-proofs.json`), not beside the scripts as in the jev prototype: outputs never go into a skill folder.
  - `forge_doctor.py --verify-route` exists (the plan defers only running it live); it delegates to `cli_media.py` in-process.
  - The CLI child environment drops every credential-like variable and the calling agent's own session variables, not only the API keys: the fakes showed a host session token leaking otherwise.
  - The ACP agent profile is generic (duration and resolution come from the request), unlike the Dusk profile that hard-coded 6 s/720p.
