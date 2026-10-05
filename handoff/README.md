# Module handoffs

Every module writes `handoff/<module-id>.md` (for example `handoff/B10-map-prop-pack.md`) before it merges. Z-docs-packaging applies them to the SKILL.md files, READMEs, CHANGELOG, `shared/` schemas and helpers, then deletes this folder (Z-T9). Write for someone who has not read your code: paste-ready text, finding ids, exact diffs.

Rules for every section:

- Commands are single lines, run from the user's project root: `python "<skill-dir>/scripts/<tool>.py" ...`. In Claude Code notes `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. No line continuations. Outputs stay in the user's project.
- Link only files that exist in your worktree; name files from other modules in plain text.
- Console text and handoff text stay ASCII-friendly; no franchise names.
- Keep all eight headings below, in this order. Write `None.` under a heading that does not apply.

## Template

Copy everything below the line into `handoff/<module-id>.md`.

---

```markdown
# <module-id>: <one-line summary>

## 1. CLIs

One line per verb, with the flags a user needs first:

    python "<skill-dir>/scripts/<tool>.py" <verb> --input <in> --output-dir <new-dir>

State what --help prints under cp1252/cp950, and which output paths the one-line JSON summary reports.

## 2. SKILL.md routing rows

Per skill, rows ready to paste into its routing table:

| Need | Route |
|---|---|
| <user need> | `scripts/<tool>.py <verb>`; then <next step> |

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `<tool>.py` | <one sentence> | <test or gate> |

## 4. CHANGELOG entries

- Added: <feature> (<task id>).
- Changed: <behaviour> (<task id>).
- BREAKING: <new default>; legacy switch `--<flag>` (plan Appendix H row).
- Fixed: <bug> (<finding ids, e.g. MAP-03, S14>).

## 5. Schema change requests

Exact diffs against `shared/schemas/<domain>.schema.json` (a unified diff, or the JSON Schema fragment plus its JSON pointer), each with the reason and the producer and consumer modules. Also list optional fields your producers add: objects are open, but undocumented fields drift.

## 6. Shared-helper promotion requests

Each private `_local_*` helper that belongs in forge_core, forge_matte, forge_av or forge_palette: name, signature, file:line, the frozen API it would extend, and its tests.

## 7. Cross-module links that Z must add

Links or routing that need another module's files, named in plain text (for example "processing.md should point to frames-and-clips.md once B02 lands").

## 8. Known limitations and what is not proven

What the tests do not cover, thresholds measured on one clip or sheet, platforms not run (macOS, Linux, Python 3.10, Pillow 10.1), editor imports not verified, and any deviation from the plan with its reason.
```
