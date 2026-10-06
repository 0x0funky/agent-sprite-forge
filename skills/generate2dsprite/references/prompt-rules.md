# Generation prompts

A prompt is a request, not a guarantee: save what you asked for and inspect the pixels that came back. Every kind of art uses the same route order. The API comes first when a key is configured, then the local daemon (the host's own image tool, Codex or Grok CLI), and codeart2d only when no route exists or the user asks for code-drawn art. The rules below are the recipe that shipped game-opus55's sprites. `master_still.py` writes the master prompts for you ([master-still.md](master-still.md)). Ready-made phase lists and grids for sheets are in [action-recipes.md](action-recipes.md).

## Characters: one master still, then one clip per action

A character is not drawn as a pose sheet. Generate and approve **one master still**. Then make each action as one image-to-video clip from that still ([video-handoff.md](video-handoff.md)). Sheets are for FX, icons and props ([below](#sheets-fx-icons-and-props)).

Write the master prompt in this order:

1. **One square image.** `Create exactly ONE square image (1024x1024).` Host tools return about 1254x1254 at 1:1. The pad normalises size and framing afterwards, so ask for the square, never for a pixel grid.
2. **The style line is the finish.** HD is the default: `premium HD 2D game art: a clean, high-resolution hand-painted sprite illustration with crisp sharp edges, a clean dark outline, rich but controlled colours, cel shading with soft highlights and clear light and shadow shapes, fine readable detail, no blur, no photo-realism, no 3D render look`. When the genre helps, name it after the style words ("... game art for a side-scrolling action game: ...").
3. **The pixel finish, only when the user asks for pixel art.** Use the line that worked: `premium 16-bit pixel art (SNES era), crisp visible chunky pixels, limited palette, strong dark outline, 3-tone cel shading, no smooth gradients, no blur`.
4. **Text bans.** `No text, no letters, no numbers, no logo, no watermark, no signature, no UI, no border, no frame, no grid.` When one mark belongs to the design, such as a crest symbol, allow that mark alone: "The ONLY written character allowed anywhere is the gold crest symbol on the helmet; nothing else carries any character, letter or symbol." Ask for abstract strokes on scrolls and banners ("faint abstract wavy brush strokes, no real characters").
5. **Reference roles, by position.** The FIRST attached image is the identity: "the FIRST attached image is the dialogue portrait of this exact character; copy his face, helmet, armour colours and his scroll exactly". The SECOND is an approved in-game peer sprite: "match its style, outline weight, shading, figure size and position on the canvas; do NOT copy his outfit". Attach the images through the tool's real image input, in that order; a path written in prose is not a reference.
6. **Subject, with the facing stated three ways.** Give the FACING label, the parts that point to the edge, and forward and back: "clearly FACING LEFT: her face, gaze and chest turn toward the LEFT edge of the image; forward is LEFT and her back is toward the RIGHT edge." Then write the long, precise identity: face, hair, body, costume colours and patterns, held items and where they point. Name what to leave out ("no pipe, no animals").
7. **Framing as canvas-edge percentages.** "the top of the figure about 14 percent below the top edge of the canvas and its lowest point about 14 percent above the bottom edge, centred, generous empty magenta margin on every side, nothing touching or crossing the edges." Each class has one framing ([master-still.md](master-still.md#class-framing)). Generators draw larger than asked (a crest tip asked at 14% arrived at 7%), so keep the margin and let the pad fix the rest.
8. **Opaque pixels on a flat key.** "The whole figure (crest, scroll, light wisps, motes) is drawn with solid, fully opaque pixels: no transparency, no semi-transparent glow, no soft aura, nothing blended with the background." End with `Background: solid flat pure magenta #FF00FF everywhere outside the character, no shadow, no ground, no glow on the background.` Purple and pink designs take a green or blue key instead.

The route's wrapper adds the tool instructions and saves the file, so the prompt carries art direction only. Generate two or three takes and choose one; do not tune one take forever.

## Fix by edit, never by re-roll

A good still with one flaw is edited, not regenerated. The approved still is the FIRST image, and any extra reference (for example a portrait for the face) comes after it with its role:

> Reproduce the FIRST image exactly: the same single figure at exactly the same size and the same position on the canvas (the top of the figure about 13 percent below the top edge, ...), the same pose, three-quarter view facing RIGHT, the same <identity recap>. Same <style> as the FIRST image.
>
> THE ONLY CHANGE: repaint his face so it matches the SECOND image ... Everything else (costume, helmet, armour, silhouette) stays exactly as in the FIRST image.

Make one change per edit. Restate the framing with numbers, then approve the result like any take: `python "<skill-dir>/scripts/master_still.py" edit --master art/master/hero --change "..." --output-dir art/master/hero-edit1`.

## Motion prompts: one action per clip

Build every clip prompt from the approved master (`master.json`):

- "The same `<identity_recap>`", verbatim, then ONE action: "keeps both feet planted, pulls the spear back a little, then shoves it straight forward to the LEFT at waist height ...; only one thrust".
- "facing LEFT the entire time and never turning around".
- "the clip starts AND ends in exactly the same pose as the still".
- "camera locked, no zoom, no pan; solid flat pure magenta background only for the whole shot; keep the exact `<finish>` look, palette and costume; single continuous action".
- The negatives collected from failed takes of that character. Typical failures: turning around; design drift (horns, hair colour, longer legs); a weapon that changes shape or doubles; a push-in of 12-40%; dust, orbs or arrows appearing; effects covering the face; garbled text; cross flares.

Registration, the take checks and keying are in [video-handoff.md](video-handoff.md).

## What not to prompt

- **No logical pixel grid.** Image models ignore "every logical pixel is an 8x8 block" (the trial fox had 5.6-6.5 px blocks). Pixel art is made after generation: registration, an area downscale of about 1/8 to 1/12 and one shared OKLab palette ([palette-and-pixels.md](palette-and-pixels.md)). Never reduce with nearest-neighbour.
- **No hex palette lists or colour caps.** Name colours in words in the identity; the palette is built from the approved frames.
- **No ban on era words for pixel art.** The 16-bit style line is used only when the pixel finish is requested; it is the line that produced the pixel art.
- **No transparency requests.** Ask for the flat key colour; keying is a separate, measured step.
- **No guides on a master.** Boxes, ground lines and stick figures belong to sheet guides only.

## Sheets: FX, icons and props

Sheets suit effects, icons, props and the rare character sheet a user explicitly wants.

- **Aspect, not pixels.** A host tool keeps the requested aspect and returns about 1,572,864 px: 3:2 gives 1536x1024, 16:9 gives 1672x941, 2:1 gives 1774x887. Ask for the aspect, describe the grid ("4 poses in 2 rows x 2 columns, read left to right, top row first"), and process the size that came back. `plan_guide.py` predicts the size and picks the grid with the most room per pose.
- **Body only, no trails.** A character's body sheet holds the body and held equipment only. Slashes, trails, muzzle flashes, impacts and dust are their own FX sheet, layered at runtime.
- **Cell containment.** Prompt for the full motion envelope inside each cell with a 15% safe margin, one scale and one root: nothing may touch or cross a cell edge, and no gutters or separators are drawn. Check the raw sheet before slicing, and slice by component ownership when a part crosses a line; never hide a crossing with a largest-component filter.
- **2:1 isometric.** Give the projection in numbers: "2:1 dimetric isometric: ground lines rise 1 px for every 2 px across; a floor tile is a diamond twice as wide as tall; vertical edges stay vertical; no perspective". Register units on the footprint centre.

### NEAR and FAR limbs, never left and right

In a side view the model cannot tell the character's left leg from its right. Name limbs by depth. **NEAR** limbs are on the viewer's side, at normal brightness, drawn in front of the body. **FAR** limbs are on the away side, one shade darker, partly hidden. Write the phases with them ("0 contact: NEAR leg reaches forward, heel down; FAR leg trails. 4 contact: FAR leg forward, NEAR leg trails") and add "frames 4-7 must not repeat frames 0-3: the leg drawn in front swaps". `sheet_qc.py frames --cycle run` checks the alternation from the NEAR and FAR shades. Front and back views can still say left and right foot.

### Checks after generating a sheet

Run these from the project root:

    python "<skill-dir>/scripts/sheet_qc.py" spill --input raw/burst-sheet.png --rows 2 --cols 2 --output-dir qc/burst-spill
    python "<skill-dir>/scripts/sheet_qc.py" frames --sheet raw/burst-sheet.png --rows 2 --cols 2 --output-dir qc/burst-frames
    python "<skill-dir>/scripts/scale_frames.py" --sheet raw/burst-sheet.png --rows 2 --cols 2 --scale-from neutral --target-body-px 48 --resampler box --emit-clips --clip-name burst --ticks 4 --output-dir out/burst-game
    python "<skill-dir>/scripts/build_animation_clips.py" --manifest out/burst-game/clips.json --output-dir out/burst-clips

`sheet_qc.py` publishes its report either way and exits 1 when a check fails; read `sheet-qc.json` before deciding. Regenerate a sheet whose part is cut or merged with a neighbour, and give it a smaller motion envelope. In Claude Code, `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.
