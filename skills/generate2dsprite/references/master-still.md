# Master still: one approved still per character

Every character animation starts from one approved **master still**. Each action (idle, walk, run, attack, jump, hurt, cast) is then one image-to-video clip made from that same still ([video-handoff.md](video-handoff.md), [video2dsprite](../../video2dsprite/SKILL.md)). Sheets stay for FX, icons and props. `scripts/master_still.py` makes the master: it writes the prompt, generates candidates, applies fixes as edits, pads the chosen still to the class framing and writes `master.json`, the record that every motion step reads.

The recipe is the one that shipped in game-opus55 (base_kanetsugu.txt, base_nobu_v2a.txt and the `_pad` stills). The prompt wording behind it is in [prompt-rules.md](prompt-rules.md).

## The flow

1. Write a spec: a JSON file, flags, or both (flags win).
2. Generate candidates: `generate --takes 3` calls generate2dmedia's `route_media.py` (API key first, then the local daemon). On a host with its own image tool, run `prompt`, generate with that tool and attach the listed references in order.
3. Look at the takes (`takes.png` shows them side by side) and pick one. Check the identity, the facing, that nothing touches an edge and that no text appears.
4. Fix a near-miss with `edit` ("Reproduce the FIRST image exactly ... THE ONLY CHANGE: ..."). Never re-roll a good still for a small fix.
5. Run `approve`. It pads the chosen still and writes `master.png` and `master.json`.
6. Make the motion clips from `master.png`, reusing `identity_recap` verbatim in every motion prompt.

Run each command as one line from the project root. `<skill-dir>` is this skill's folder (`${CLAUDE_SKILL_DIR}` in Claude Code).

```text
python "<skill-dir>/scripts/master_still.py" prompt --spec art/specs/fox.json --output-dir art/master/fox-prompt
python "<skill-dir>/scripts/master_still.py" generate --spec art/specs/fox.json --takes 3 --output-dir art/master/fox-gen
python "<skill-dir>/scripts/master_still.py" pad --run art/master/fox-gen --take 2 --output-dir art/master/fox-pad2
python "<skill-dir>/scripts/master_still.py" edit --run art/master/fox-gen --take 2 --change "make the cloak dark blue" --takes 2 --output-dir art/master/fox-edit1
python "<skill-dir>/scripts/master_still.py" approve --run art/master/fox-edit1 --take 1 --output-dir art/master/fox
python "<skill-dir>/scripts/master_still.py" approve --run art/master/fox-prompt --still art/inbox/fox.png --route host-image_gen --output-dir art/master/fox
python "<skill-dir>/scripts/master_still.py" edit --master art/master/fox --change "repaint the face to match the SECOND image" --extra-reference art/refs/fox-face.png --extra-role "her portrait: it shows her real face" --output-dir art/master/fox-edit2
```

Every command writes a new `--output-dir`: it refuses an existing one, stages its work beside it and publishes only a finished folder. It prints one ASCII JSON line. Errors print `error: ...` and exit 1; usage errors exit 2; `generate` and `edit` exit 3 when no route exists.

## The spec

`generate2dsprite.master_spec.v1`. Relative paths in a spec file are relative to that file. Flags use the same names with dashes (`--facing-parts`, `--identity-ref`, ...). The reference details are `--identity-ref-what`, `--identity-copy`, `--style-ref-what` and `--style-dont-copy`. `--identity-file` reads a long description from a UTF-8 file, and `--ban` adds to `extra_bans`.

| Field | Meaning | Default |
|---|---|---|
| `name` | asset id: 1-64 letters, digits, `.`, `_`, `-` | required |
| `subject` | short noun phrase: what it is ("a young fox ranger, full body") | required |
| `identity` | long, precise visual description: face, hair, body, costume colours, held items | required |
| `pose` | stance or pose of the still | none |
| `recap` | identity recap for motion prompts, a noun phrase that reads after "The same" | `subject (identity)` |
| `facing` | `left`, `right`, `front`, `back`, `none` | `right` (props `none`) |
| `facing_parts` | parts named in the facing sentence | `face, gaze and chest` (props `front`) |
| `view` | camera words | `side view (profile to three-quarter)` for left and right |
| `class` | framing preset: `hero`, `mob`, `boss`, `prop` | `hero` |
| `finish` | `hd` or `pixel`: sets the style line | `hd` |
| `key` | flat key colour (`#RRGGBB`, `magenta`, `green`, `blue`) | `#FF00FF` |
| `genre` | the game, written into the style line ("for a side-scrolling action game") | none |
| `pronoun` | `he`, `she`, `it`, `they` | `they` |
| `identity_ref` | `{path, what, copy}`: the FIRST attached image, copied exactly | none |
| `style_ref` | `{path, what, do_not_copy}`: an approved in-game peer sprite; an empty `do_not_copy` drops that clause | none |
| `allowed_text` | the one written mark allowed (a crest symbol); all other text stays banned | none |
| `extra_bans` | extra "no ..." items | `[]` |
| `opaque_parts` | parts named in the opaque-pixels sentence ("hair, spear, flames") | none |
| `extra` | one more art-direction paragraph, placed before the background line | none |

On PowerShell, quote a hex key (`--key "#00FF00"`), or the `#` starts a comment; `--key green` works everywhere. Choose green or blue for purple and pink designs. `pad` and `approve` warn when more than 0.5% of the subject's colours lean to the key.

## What the prompt says

`build_prompt` writes the game-opus55 contract, one paragraph per part:

1. `Create exactly ONE square image (1024x1024).` Host tools return about 1254x1254; the pad normalises the size.
2. The style line of the finish, then the text bans: `No text, no letters, no numbers, no logo, no watermark, no signature, no UI, no border, no frame, no grid.`
3. The reference roles: the FIRST attached image is the identity ("copy ... exactly"). The SECOND is an approved in-game peer sprite: "match its style, outline weight, shading, figure size and position on the canvas; do NOT copy ...".
4. The subject with the facing stated three ways (`clearly FACING LEFT:`, the parts turned toward the LEFT edge, forward and back), then the identity and the pose.
5. The framing as canvas-edge percentages, "nothing touching or crossing the edges", and "solid, fully opaque pixels: no transparency, no semi-transparent glow, no soft aura".
6. `Background: solid flat pure magenta #FF00FF everywhere outside the character, no shadow, no ground, no glow on the background.`

The route adds its own tool instructions and saves the file, so the prompt holds art direction only.

## Class framing

Each class has one framing, identical for every character of that class. The prompt asks for some margin, because generators draw larger than asked. The pad then moves the subject to the exact target on the 1024x1024 canvas.

| Class | The prompt asks for | Pad target | Evidence |
|---|---|---|---|
| `hero` | top 14%, bottom 14% (72% tall), centred | 788 px tall, top margin 138 px, centred | base_kanetsugu_pad, base_chiyome_pad |
| `mob` | top 35%, bottom 15% (50% tall), body behind centre | 471 px tall, top margin 406 px, centre 100 px away from the facing side | base_ashigaru_pad, base_akazonae_pad |
| `boss` | top 14%, bottom 8% (78% tall), centred | 728 px tall, top margin 221 px, centred | base_kage_pad |
| `prop` | top 20%, bottom 20% (60% tall), centred | 614 px tall, top margin 205 px, centred | none |

These are generation framings. Display sizes come later: one body height per character, mobs no taller than the hero, a boss about twice the hero. `--subject-height`, `--top-margin` and `--lead` override a preset; the record then says `"preset": "custom"`.

## generate: routes and takes

`generate` writes `prompt.txt`, `spec.json` and `run.json`, then runs once per take:

```text
python "<generate2dmedia>/scripts/route_media.py" image --prompt-file <run>/prompt.txt --reference <identity> --reference <peer> --size 1024x1024 --out-dir <run>/takes/take-NN [--route R] [--media-arg values]
```

- The last JSON line of its stdout decides the take. `{"status": "ok", "route": ..., "artifact": path, ...}` keeps the artifact; an artifact outside the take folder is copied into it. Exit 3 (or `"status": "no-route"`) stops the run: the output keeps the prompt, the summary says `"status": "no-route", "fallback": "codeart2d"` and the command exits 3. Any other failure is recorded for that take.
- The run is `ok` when every take succeeded and `partial` when some did (both exit 0). It is `fail` when none did: the folder is still published for diagnosis, and the command prints `error: every take failed (...)` and exits 1.
- Takes run one after another. The media CLI's stderr (estimates, warnings) is echoed live with a `[take N]` prefix; anything shaped like an API key or bearer token is replaced by `[redacted]` there and in `run.json`. `takes.png` shows two or more good takes side by side.
- `--route` is passed through. `--media-arg=--flag` passes any other argument (write it with `=`). `--timeout` stops one take (default 1800 s).
- `route_media.py` is found beside this skill (`../generate2dmedia/scripts/route_media.py`). `--media-cli PATH` overrides it, and `FORGE_ROUTE_MEDIA_FAKE=<script.py>` substitutes a fake for tests. A fake is recorded in `run.json` (`media.cli_source`) and is printed as a warning; `approve` also warns, so a fake take is never mistaken for a real generation.

No route at all: run `prompt`, generate with the host's own tool (attach the references in the order of `run.json` `references`), then run `approve --run <prompt dir> --still <image> --route <what made it>`.

## edit: fix by edit

`edit` writes the base_nobu_v2a.txt prompt:

```text
The FIRST attached image is the approved master still on a flat magenta #FF00FF background: <subject>. The SECOND attached image is <role>.
Reproduce the FIRST image exactly: the same single figure at exactly the same size and the same position on the canvas (<measured framing>), the same pose, <view>, facing LEFT, the same <identity_recap>. Same <style> as the FIRST image: <traits>.
THE ONLY CHANGE: <change>. Everything else (<keep>) stays exactly as in the FIRST image.
```

- The FIRST image is `--master` (its `master.png`, spec and recap), a run take (`--run --take`) or `--still`. Each `--extra-reference` follows it, in order, with its `--extra-role`.
- The FIRST image's framing is measured and written as canvas-edge percentages.
- `--prompt-only` writes the prompt and `run.json` without generating, for the host's own image tool.
- Make one change per edit, and approve the result like any take.

## pad

`pad` keys the still with forge_matte: the key is estimated from the border ring (generated backdrops drift, for example (252, 3, 251)), and the soft matte un-mixes the edges. It then:

- builds the subject box from every part with at least 24 px of alpha > 16, so the specks are dropped;
- crops the subject with a 6 px margin;
- places it on the 1024 canvas at the class framing: premultiplied LANCZOS, or box below 0.5x, and the top of the box lands exactly on the top margin;
- clears the alpha 1-4 ringing and composites the result over the pure key.

Every pixel outside the subject is then exactly the key colour. It writes `padded.png` (opaque RGB), `padded_rgba.png` and `pad.json` (`generate2dsprite.master_pad.v1`: framing, transform, matte QA, key choice, warnings).

Re-padding game-opus55's raw kanetsugu still with `--class hero` gives the subject box (208, 138, 816, 926), against (208, 138, 815, 927) for the hand-made pad, with a mean pixel difference of 3.1. Re-padding the ashigaru still with `--class mob --facing left` gives (257, 406, 966, 877), against (259, 405, 970, 878).

Warnings: the subject touches an edge of the still, so it may be cut; the subject was upscaled, so detail is soft; the subject is too wide for the class framing, so it was scaled to fit the width; the subject's colours lean to the key; opaque key-coloured pixels remain. The pad refuses a still that has no flat key backdrop and no real alpha.

## approve and master.json

`approve` takes the chosen still (`--run --take`, or `--still` with `--run` or `--spec`), pads it and writes:

| File | Content |
|---|---|
| `master.png` | 1024x1024 RGB at the class framing; every pixel outside the subject is the pure key. This is the image-to-video input. |
| `master_rgba.png` | the same placement with transparency (the cut-out) |
| `source.<ext>` | a byte copy of the chosen still |
| `spec.json` | the spec, with `recap` set to the final identity recap |
| `prompt.txt` | the prompt that made the still, when known |
| `master.json` | `generate2dsprite.master.v1` |

`master.json` fields. These names are stable; motion steps read them and never re-measure the still:

| Field | Meaning |
|---|---|
| `schema` | `"generate2dsprite.master.v1"` |
| `tool`, `created` | `{name: "master_still", version}`, RFC 3339 UTC time |
| `name` | asset id |
| `subject` | the short noun phrase |
| `identity_recap` | long, precise visual description, a noun phrase reused verbatim after "The same" in every motion prompt |
| `facing`, `view` | `left`/`right`/`front`/`back`/`none`; camera words |
| `finish` | `hd` or `pixel` |
| `class` | `hero`, `mob`, `boss` or `prop` |
| `framing` | `canvas` [1024, 1024], `subject_height_px`, `top_margin_px`, `lead_px`, `preset`; measured on `master.png`: `subject_bbox` [x0, y0, x1, y1], `anchor` (stance midpoint on the feet line), `opaque_area_px` (alpha >= 128), `width_limited` |
| `key` | `"#FF00FF"` (upper-case hex) |
| `files` | `master`, `cutout`, `source` (fileRefs with `path`, `sha256`, `bytes`, `size`), `spec`, `prompt` (fileRef or null); paths are relative to `master.json` |
| `references` | `[{role: identity or style, path, sha256, bytes, what}]` in attachment order |
| `transform` | `master = scale * source + offset` in continuous pixel coordinates: `scale`, `offset` [x, y], `source_size`, `source_subject_bbox`, `crop_box`, `crop_margin_px` (6), `canvas`, `resampler`, `key_rgb`, speck counts |
| `route` | the route that made the still (from the take, `--route`, or `external`) |
| `provenance` | `command` (generate, edit, prompt or external), `run`, `take`, `source_sha256`, `media` (the route's summary without local paths), `media_cli_source`, `edit` (`from`, `change`) |
| `matte` | keyer, key estimate, matte QA and key choice |
| `warnings` | every warning, verbatim |

A motion prompt built from it: "The same `<identity_recap>`, `<one action>`, facing LEFT the entire time and never turning around; the clip starts AND ends in exactly the same pose as the still; camera locked, no zoom, no pan; solid flat pure magenta background only for the whole shot; keep the exact `<finish>` look" plus the negatives collected from failed takes ([prompt-rules.md](prompt-rules.md)).

## Limits

- The pad measures framing; it does not judge identity, anatomy or facing. Look at `master.png` before you approve.
- A reference on another drive is stored by file name and sha256 only (manifests never hold absolute paths). Keep the references inside the project.
- Without a route, nothing is generated and nothing is claimed: the summary says `no-route`, and codeart2d ([SKILL.md](../../codeart2d/SKILL.md)) is the fallback. Say "code-drawn, no image model" when you use it.
