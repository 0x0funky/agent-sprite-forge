# Keying video frames (matte)

`video2dsprite.py clean` and `process` turn chroma-backdrop frames into straight-alpha
RGBA frames (`clean_0000.png` onwards) and write `matte-report.json` beside them. The
report is a `video2dsprite.matte_report.v1` document and a QA envelope (status, method,
notProven, checks, hashed inputs and outputs). `process` also copies it, without the
file lists, into `pipeline-meta.json` as the `matte` block.

Commands run from your project root; `<skill-dir>` is the skill folder
(`${CLAUDE_SKILL_DIR}` in Claude Code). Every verb writes a new `--output-dir`
(`--out-dir` is the old name): it must not exist, is built in a staged folder beside
it and appears only when the run succeeds. Progress and warnings go to stderr; stdout
is one JSON line naming the output and its metadata file.

## Workflow

1. Plan the key before generating the clip:

   ```text
   python "<skill-dir>/scripts/video2dsprite.py" key-plan --master art/hero-master.png
   ```

   It prints the key (`magenta`, `green` or `blue`, first acceptable in that order)
   and `background_sentence` for the video prompt. A key is rejected when more than
   0.5% of the master's subject pixels lean to it by 40 or more, so a purple, violet
   or pink design rejects magenta. With an RGBA master it also lists
   `design_colours_at_risk`: colours the video matte would still key out with that
   key. `--strict` exits 1 when every candidate fights the design.

2. Triage the raw clip before processing it:

   ```text
   python "<skill-dir>/scripts/video2dsprite.py" triage --video raw/hero-idle.mp4 --output-dir work/hero-idle-triage
   ```

   `raw-triage.json` (`video2dsprite.raw_triage.v1`) reports size, rational fps,
   decoded frames, audio and cover-art streams, `borderTouchFrames` and `keyDrift`;
   `triage-sheet.png` shows 12 evenly spaced frames with border touches outlined.
   A subject touching the border inside the action you need is a cropped limb, weapon
   or prop: regenerate the clip, never pad it. `--strict` refuses such a clip.

3. Key it, in one step or two:

   ```text
   python "<skill-dir>/scripts/video2dsprite.py" process --video raw/hero-idle.mp4 --output-dir work/hero-idle --start 1 --duration 2 --reference art/hero-master.png
   python "<skill-dir>/scripts/video2dsprite.py" extract --video raw/hero-idle.mp4 --output-dir work/hero-idle-raw --start 1 --duration 2
   python "<skill-dir>/scripts/video2dsprite.py" clean --raw-dir work/hero-idle-raw --output-dir work/hero-idle-clean --reference art/hero-master.png
   ```

   Raw frames are `frame_000000.png` onwards (0-based); `--start/--duration` keep the
   frames timed in [start, start + duration).

4. Read the report's `status`, `checks` and `warnings`, then look at the frames over a
   light and a dark background before packaging them:

   ```text
   python "<skill-dir>/scripts/video2dsprite.py" package --clean-dir work/hero-idle/frames-clean --out-dir assets/hero-idle --name hero-idle --fps 24 --formats png,webm,packed --loop
   ```

## Matte modes (`--matte`)

| Mode | What it is | Use it for |
|---|---|---|
| `soft` (default) | Known-key soft matte (forge_matte v13): matting in a luma-weighted space because H.264/VP9 frames are 4:2:0, edge colour un-mixed from the key, enclosed key holes keyed as background | every generated clip |
| `dominance` | The fast key-dominance ramp of the Dusk clips: alpha falls as a colour leans to the key | quick previews; subjects with no key-leaning colours (any colour leaning more than 20 turns partly transparent; faint tint stays on opaque edges) |
| `binary` | The cfed170 border-flood keyer, bit for bit: binary alpha, magenta only, `--dist` and `--despill` strength | reproducing old output |

Enclosed key-coloured pockets are removed after the matte (`--pockets auto`: on for
soft and dominance, off for binary so the legacy keyer stays exact; `remove` or
`keep` to choose). Pocket removal clears only regions close to the key colour, so a
purple costume is never dug out by it. `--erode N` shrinks the matte N px (keep 0
unless a halo needs it; profiles pin it per character).

## Key (`--key`)

- `auto` (default) finds which of magenta, green or blue the border ring of five
  sampled frames shows, then estimates that key per frame from the border ring
  (generated backdrops drift: Grok JPEG (235, 21, 175), image-to-video (202, 80, 177)).
  A frame whose estimate fails uses the clip median and is listed in the report.
- `magenta`, `green`, `blue`: the named key, estimated per frame.
- `#rrggbb`: used as given on every frame (no estimate).
- `--key-mode auto` keeps frames that already have real transparency (VP9 alpha WebM);
  `always` keys every frame; `none` copies frames; `magenta` is the old spelling of
  `always` with the magenta key.

## Despill (`--despill-mode`)

- `auto` (default) decides once per clip: when the reference still (`--reference`) and
  the median of 16 evenly spaced frames both have at most 0.5% key-coloured material,
  the subject owns none, so every key tint inside it was painted by the generator and
  is removed (`all`); otherwise only edges are cleaned (`edge`). `process` with
  `--start/--duration` samples the whole source clip, so a window cannot flip the
  decision; `clean` judges the frames it is given. The report's `interior_despill`
  records the shares, the sampled frames and where they came from.
- Soft matte: `all` clamps key excess on every visible pixel, `edge` only in its
  6 px colour band (a purple subject keeps its purple), `off` disables both.
- Dominance and binary: a despill pass (`all`, or `edge` within `--despill-radius`
  px of transparency, default 1) runs after the residue QA.
- A matte profile can pin the decision with `params.interior_despill`.

## Protecting design colours

The video soft matte keys light colours near the key even inside the body: #b43cc8,
(180, 60, 200) and #972fbf against magenta all reach alpha 0. Dark purples such as
(120, 40, 140) survive. Three remedies, best first:

1. Plan a key the design does not fight (`key-plan`), so the clip is generated on green.
2. `--protect-color #b43cc8` (repeatable; OKLab tolerance `--protect-tol`, default
   0.03): protected pixels are never keyed, despilled or removed as pockets.
3. Recolour the master with the suggestions in `design_colours_at_risk`.

`--reference` (the RGBA master, or the image-to-video input still on the key, which is
keyed with the still parameters first) makes `clean`/`process` name the at-risk
colours in a warning and in the report's `design_colours_at_risk`. When the clip owns
key-coloured material and nothing is protected, the run warns too.

## Temporal stability (`--temporal-stability`)

`auto` (default) runs an alpha hysteresis for soft and dominance alpha and nothing for
binary alpha. Each pixel keeps an opaque or transparent state that changes only when
alpha leaves the 0.4-0.6 band on the other side; while it holds, alpha moves at most
0.25 per frame. A decisive change passes at once and alpha 0 is never raised. The
report's `temporal` block records flips per frame pair before and after (pixels whose
alpha changed by more than 0.25 while the raw colour changed by at most 20); on the
Ryo clip 10.1 become 3.6. The cost: in-band alpha changes lag by up to one frame.
`off` keys every frame alone. `--local-background [PX]` mattes against the local
backdrop mean (12 px), which helps edges over a drifting or vignetted backdrop and
costs about 0.4 s per 960x960 frame. An eroded silhouette that moves counts as flips
in this metric, so read the flips check together with `--erode`.

## Matte profile (`--matte-profile`)

A character profile (`video2dsprite.character_profile.v1`) pins `mode`, `key`,
`erode`, `unmix` and `despill`, plus optional soft-matte `params`, so every clip of a
character is keyed the same way:

```text
python "<skill-dir>/scripts/video2dsprite.py" clean --raw-dir work/hero-attack-raw --output-dir work/hero-attack-clean --matte-profile art/hero-profile.json
```

A flag that contradicts the profile wins, prints a warning and is recorded in the
report's `profile.deviations`, because that clip's matte now differs from the others.
`unmix` belongs to the soft matte (`--no-unmix` takes edge colour from the
neighbouring subject only).

## Gates and the report

| Check | Gate | Meaning |
|---|---|---|
| `opaque_key_px` | 0 | pixels with alpha >= 128 within RGB 48 of the key |
| `enclosed_key_pockets` | 0 | visible key-coloured regions of 16 px or more |
| `outer_ring_spill_fraction` | <= 0.01 | share of the outer visible ring leaning more than 20 to the key (the cfed170 keyer measured about 0.70) |
| `flips_per_frame_pair` | <= 5.7 warns only | the sprite-gen default on the Ryo clip; a reference, not a law |

Residue counts are taken before any despill pass, which could otherwise hide a pocket
as an opaque grey blob. A failed check makes the status `fail`: the frames are still
published with a warning so you can review them, and `--strict` refuses them instead
(nothing is published). Passing numbers give `needs-visual-review`, never `pass`:
matte QA cannot judge edges, colour or identity. Counts are sums over frames, fractions
the worst frame; per-frame rows are in `frames`. The report never stores timings, so
the same frames and options give the same bytes.

## Legacy switch

`--matte binary --despill-mode off` reproduces the cfed170 keyer pixel for pixel
(RGB under alpha 0 is zeroed in every output PNG). The defaults it replaces are the
soft matte with pocket removal and auto despill. `--dist` and `--despill` (first-ring
strength 0..1) belong to the binary matte only and are refused elsewhere.

## Limits: one clip

The soft matte, the auto despill rule (0.5%), the 0.01 ring gate and the 5.7-flip
reference were tuned and verified on one clip: Ryo, magenta key, black-outlined
cartoon, H.264 4:2:0, 960x960, 145 frames (report v2 section 7). Green and blue keys,
outline-free art, other codecs and fast motion are covered by synthetic tests only.
On that clip the default run measured fringe 0, leak 0, no enclosed pockets in 145
frames, 3.6 flips per frame pair (sprite-gen best: 1.1) and about 0.7 s of keyer time
per frame. Narrow gaps filled with bright key glow can stay opaque (about 170
key-hued pixels per Ryo frame), and rims under about a third coverage of a dark
outline are eroded. Look at the frames.
