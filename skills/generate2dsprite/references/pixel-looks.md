# Pixel looks: characters as real, small pixel sprites that follow a reference

An opt-in route for a game whose characters are classic pixel art. Use it only when the user asks for that kind of
sprite. It is not a default and it changes nothing else: the HD, pixel and hibit finishes work exactly as before.

**The look is never described in words. It comes from a picture.** One sprite the user likes is the reference of
the whole cast: its manner, the size of its pixels, its build. The words of every prompt fix only the format (one
figure an image, large blocks, a flat key colour). Words about a style (counts of pixels, "flat", a number of
tones) are not obeyed and take the look away; a reference is followed.

Three things are the user's to accept, in this order, each before the next is made: **the manner** (the style
picture and the first sprite drawn after it), **the master** (the 1x sprite), **the first motion set** (idle, move,
attack). Show the generated picture beside what was made from it, every time.

## Why this is its own route

An image model cannot place pixels. Asked for pixel art it paints squares that are not all one size, on no exact
grid. Shrinking that picture gives mud. A whole sprite sheet drawn freely has the look, but its figures are small:
their cells are a few px, soft, on a dark ground with a shadow, and no clean edge can be cut from them. So the
picture is never the source of the pixels of a small figure. A figure is drawn **again, alone and large**, and that
picture's own grid is measured and followed (`forge_pixelgrid.snap_picture`); its outline is then closed by rule.
Everything else of the character is made **from that master on the master's own lattice**, so the build cannot
change from one pose to the next. A video model moves the figure off any grid; the motion step reads every frame
back on the sprite's lattice and palette.

## 0. The style picture (the manner)

The reference is a picture of **one figure in the manner the user wants**:

- a sprite of the user's own, or one made here before (its `<name>-how.png`), when a second look is started from it;
- or a figure cut from a style picture. To make one, give the image route the character's design picture and ask
  in plain words, **the user's own words**, for a sheet of small battle sprites of that character in the manner
  they name. Do not add sizes, tone counts or technique: plain words give the manner, prescriptions flatten it.
  Let the user pick the figure they like and cut it out (any size; it is only looked at).

A reference is used to show a manner. Use pictures the user has the right to use: a sprite cut from a published
game is that game's art, good for studying a manner in private and not for shipping. The reference stays where the
user keeps it (the commands record its path and copy nothing). The prompts these commands write name no title, and
a look is never named after one.

## 1. The master (`pixel_look.py ask-master`, `master`)

1. `python "<skill-dir>/scripts/pixel_look.py" ask-master --how <the liked figure> --subject "<who, one sentence>" --output-dir <new>` writes `prompt.txt` and `request.json`: the figure drawn again, once, large and centred, on a flat key colour the figure does not have (`--key auto` picks it; `magenta`, `green`, `blue`).
2. Generate: `python "<skill-dir>/../generate2dmedia/scripts/route_media.py" image --prompt-file <new>/prompt.txt --reference <the liked figure> --size 1024x1024 --out-dir <take>`.
3. `python "<skill-dir>/scripts/pixel_look.py" master --image <take>/generated.png --name hero --output-dir <sprite dir>` writes `hero.png` (the 1x sprite), `hero-how.png` (the sprite on one exact grid on its key colour), `hero-review.png` (the picture beside the sprite), `hero-grounds.png` (the sprite on a pale, a grey and a dark ground; `--ground #rrggbb` adds the game's) and `hero.json`.
4. **Look at `hero-review.png` and at the edge on every ground in `hero-grounds.png`.** A take that misses is generated again; nothing is repaired by hand.

What `master` does: every cell of the picture's own grid gives one colour (no merging of near colours: closest-pair
merging runs navy, brown and black together); what is left of the key colour goes (edge cells mixed with it, an
outline cell that the ground half covers is an outline cell again, a gap of a cell or two inside the figure that
is still key is opened; one such cell left in would be a colour of the palette, and every edge cell of a video
frame that compression tints with key would be read as it); the outline is closed by rule (a dark speck that hangs by one side goes, the line is drawn **outside** a bare lit edge cell so the
figure loses nothing, every dark edge cell takes the one line colour); the colours come down to `--colors` (48) by
k-means in OKLab, so every hue keeps a place. A picture whose cells are under 8 px is refused: ask for one figure,
large.

**A look without an outline** (lit edges, as a figure rendered from a 3D model has them): give `--outline drawn` to `ask-master` and `master`, and to `ask-cast`, `ask-pose` and `ask-view` (the reading commands follow their request). The prompts then ask for the same edges with no outline added, the rule that closes a dark outline is left out, and the edge is kept as the picture has it; the key colour is still cleaned from it, down to what the package's residue gate measures (an edge cell whose key channels lead the others is unmixed). Such a sprite has no line to hide a fringe: look at its edge on the game's own ground (`--ground`), and give `--outline drawn` to `pixel_motion.py prepare` too and leave `--closed-outline` out of `finish`.

**The figure keeps the proportions it has in the picture.** An image model's blocks are often not square: drawn
after a free picture they came back a tenth to a fifth taller than wide (9.7 x 11.1 px, 20 x 22 px, 41 x 49 px);
drawn after a sprite on an exact grid they came back square. One pixel a block would then give a figure that is
flatter than its picture by that much, and every view and motion made from it would be as flat. So `master`
measures the blocks (`blockPx` in the record's `read.proportions`) and, where they differ by more than 4 percent,
doubles the rows (or, for blocks wider than tall, the columns) that show least: the figure is cut into as many
stretches as rows are wanted and one row of each is doubled, the one across flat colour and upright outline, not
the one across an eye or a belt. Nothing is dropped or redrawn, and the outline stays one cell thick (the inside
grows under it). `rowsDoubled` in the result says how many; `--proportions blocks` gives one pixel a block as it
comes (the size a sprite made before this had). **In `hero-review.png` the sprite must have the picture's
proportions: a master that is flatter or thinner than its picture is not accepted.**

The sprite's size is what the picture had, not a number from a prompt (a figure asked to fill three quarters of a
1024 px picture came back with cells of about 20 px: sprites of 33 and 43 columns). The picture is not on an exact
grid, so the master is a little simpler than its picture: fine detail (a clasp, a braid, the far eye) can be lost.
Say so when showing it.

## 2. Every further character (`ask-cast`, `cast`)

A character drawn alone "in the manner of the first" keeps the manner and loses the size: its pixels come back smaller, so the figure is a tenth to a quarter taller than the first master (62 rows beside 50 in one take). A cast stands together only when every character is drawn **beside the first master in one picture**:

1. `pixel_look.py ask-cast --master <sprite dir>/hero.png --who <a picture of the new character> --subject "<who, one sentence>" [--pose "<the view and the stance>"] [--size "<how tall beside the master, in words>"] --output-dir <ask>` writes `reference.png` (the master at the left of a 1536 x 1024 picture, on a ground line near the bottom, the rest empty), `prompt.txt` ("THE ONLY CHANGE: in the empty area to the right, add a SECOND figure: the character of the SECOND image", on the same pixel grid, as tall as the first figure) and `request.json`. The key colour is one that neither figure has.
2. Generate with `--reference <ask>/reference.png --reference <the picture of the new character> --size 1536x1024` (the references in that order).
3. `pixel_look.py cast --image <take>/generated.png --request <ask> --name mage --output-dir <sprite dir 2>`: the first figure must come back as the master and gives the cell size; the new figure is read with that cell size on its own lattice, keeps its own colours (`--colors`), and its outline is closed in the first master's line colour. Every part of some size is taken (a staff held apart from the body), specks are not. It writes the same files as `master`; `mage-review.png` shows the generated picture above the two figures on one ground line, and `heightRows` beside `masterHeightRows` with a warning says when a figure asked to be as tall is not.

The body's height is measured in a narrow column over the feet, so a staff, a blade or a helmet's horns beside the head do not count. A monster that should be larger gets `--size` ("about one and a half times as tall as the first figure") and, for a wide or tall figure, a smaller `--cell` (more room).

**A reference that already is a clean 1x sprite** (the user's own sprite, or one from a sheet they have the right to use) needs no `ask-master`: give it as the `--master` of `ask-cast`, and the user's character is drawn beside it at its pixel size. The reference is then only the ruler; nothing of it is in the new sprite's files but its line colour.

**A very small look** (sprites of about 16 to 24 pixels, a few flat colours):

- Asked in words, an image model draws such a figure about twice as fine as wanted. The pixel size comes from a ruler: a clean 1x sprite of the wanted size as the `--master` of `ask-cast` (the ruler came back cell for cell, the new figure 20 rows tall beside its 17).
- Give large cells: `--cell 24` to `32` on `ask-cast` and `ask-view` (the default is right for sprites of 30 to 60 pixels). At 32 px a side and a back view read at once. Where a new figure is not on the ruler's grid and reads muddy, cut it out of the picture and take it through `ask-master` and `master` (cells of 40 px and more).
- `master --colors 12`: with 16 colours or fewer, hue counts more than lightness and every colour is one the picture had. Under about 12 a pale skin tone can still be lost among greys: look at the face.
- Motion: at this size a face boils in a clip, so lock the head (`finish --head-rows`, about half the rows of a big-headed figure) and take two drawings a step (`measure --per-step 2`). A walk toward or away from the viewer has no feet that travel: its speed is set by eye. Do not write "tiny" into `--subject`: the preparation warns that it shrinks the motion.

## 3. A pose, a weapon, an expression (`ask-pose`, `pose`)

Never ask for a pose as a picture of its own: a separately drawn pose comes back taller, thinner, with another
head. A pose is **the master reproduced with one change**:

1. `pixel_look.py ask-pose --master <sprite dir>/hero.png --change "<what is different and where, in plain words>" [--room L,T,R,B] --output-dir <ask>` writes `reference.png` (the master on an exact lattice, with empty room for what the change adds), `prompt.txt` ("Reproduce the attached image ... THE ONLY CHANGE: ...") and `request.json`. Give room on the side the change reaches to; less room gives larger cells.
2. Generate with `--reference <ask>/reference.png --size 1024x1024`.
3. `pixel_look.py pose --image <take>/generated.png --request <ask> --name hero-sword --output-dir <new>`: the picture is read on the master's own lattice (its cell size and place are searched until most of the master's cells come out equal; image models give the figure back with about 0.90 to 0.998 of its cells equal), and **only the changed part is taken**: the new cells outside the silhouette and the differing cells that hang together with them, cleaned of the key colour as a master is. Every other cell is the master's own cell. `masterKeptShare` in the result says how much of the master is in the pose; `hero-sword-review.png` shows both.

Say which side of the picture things are on ("the hand on the viewer's left"), and read the master's facing from the
picture, not from memory. A sprite with its weapon in the hand is the one to animate: make it before the motion.

## 4. Another view (`ask-view`, `view`)

A move across the screen needs a **side view**: a figure whose body faces the viewer cannot step, its legs only
open and close. A turned view is drawn **beside the master in one picture**, so that the master is the ruler:

1. `pixel_look.py ask-view --master <the sprite to turn> --view side-right [--detail "<what is seen in this view>"] --output-dir <ask>` (views: `side-right`, `side-left`, `back`, `front`, or `--view-words`): the master on the left half of a 1536 x 1024 reference, the right half empty, and "THE ONLY CHANGE: in the empty right half, add a SECOND figure: the SAME character" in that view, exactly as tall.
2. Generate with `--reference <ask>/reference.png --size 1536x1024`.
3. `pixel_look.py view --image <take>/generated.png --request <ask> --name hero-side --output-dir <new>`: the left figure gives the cell size (it must come back as the master), the right figure is read with that cell size **on its own lattice** (the second figure's cells can lie half a cell beside the first one's; `ownLattice` in the record says by how much), its colours go to the master's where one is near, and its outline is closed by the same rule. `heightRows` beside `masterHeightRows` and a warning say when the view is not the master's height: generate again.

Views drawn in one picture keep one height and one pixel size; views drawn one a picture do not. Walking the other
way is the mirrored clip (`pixel_motion.py finish --flip`).

## 5. The motion (video2dsprite `pixel_motion.py`)

- **Idle: `breath`**, no clip. `python "<skill-dir>/../video2dsprite/scripts/pixel_motion.py" breath --sprite <sprite> --name hero_idle --output-dir <set>`: two drawings, the sprite and the sprite with its upper part one cell lower (one row of the lower legs is taken out; what hangs from the hand comes down whole), 620 and 520 ms, packaged like every other set. A video model's breath is smaller than one sprite pixel, and where its clip does move, the pose and the face are redrawn; this idle keeps every cell.
- **An attack and other actions: an image-to-video clip put back on the grid.** `pixel_motion.py prepare --sprite <the sprite with its weapon> --action attack --facing <as the sprite looks> --subject "<who>" --extra "<the strike in plain words; the hand grips the weapon for the whole clip>" [--scale 8] --output-dir <job>`, the clip from `<job>/prompt.txt` and `<job>/input.png` (route_media.py video, 6 s, 720p), `pixel_motion.py frames --clip <mp4> --job <job> --output-dir <frames>`, **look at `frames-sheet.png`**, then `pixel_motion.py finish --frames-dir <frames> --name hero_attack --pick a,b,c,... --ms-list ... --loop oneshot --event hit=<ms> --closed-outline --output-dir <set>`. A smaller `--scale` gives the strike room on the side it goes to. The root is the middle of the feet, so a weapon at the side does not move the figure in the game. The clip's key colour is the one the sprite's own edge leans to least (a purplish edge on a magenta ground would be counted as key residue in every frame, and the package would refuse the set). Leave out drawings in which the weapon goes under the ground line. `--action hurt` and the other templates of `prepare_i2v_input.py` work the same way.
- **A walk: from the side view, measured before it is picked.** `prepare --action walk --facing right` from the side sprite (say in `--extra`: a profile for the whole clip, the near and the far leg swing forward in turn, the hand grips the weapon), the clip, `frames`, then `pixel_motion.py measure --frames-dir <frames> --output-dir <measure>`: the footfalls (where the front edge of the feet is farthest forward; a staff set on the ground is left out, and when a blade's tip drags in front, the toe-offs at the back edge are taken), the length of a step (checked against the lags at which the clip is like itself), the frames that only repeat the one before (a clip "on twos"), the ground speed in game pixels a second, the pair of steps that closes best with an even rhythm, and the drawings and durations to take (four a step, each for the time it holds in the clip: `finish` in the result is ready to paste). Under a long robe the feet give no steady footfalls: the step is then read from the lags alone and a warning says so. **Look at `legs.png`**: the leg in front at one footfall is the leg behind at the next. Then `finish ... --pick ... --ms-list ... --loop cycle --closed-outline --head-rows <rows of the head>`. Move the sprite in the game at the measured speed: slower and the feet slide, faster and they skate. The speed is read to about a tenth (48 for one walk, 66 to 72 for another clip with longer strides): set it by eye around that number.
- **The head lock** (`--head-rows R` or `--head-box X0,Y0,X1,Y1`): where a frame's head is already nearly the sprite's head, the sprite's own head is put there, so a face never boils. For walks; not for attacks (a blade passes in front of the head) and not where the head really turns.

A character's first set: an idle, a walk from its side view, its attack. Hit and down are further clips.

Every set's `anchor` is the middle of the feet on the ground line, in the set's own frame. It can lie on a half pixel (feet an even number of pixels wide): place every set of a character by one rounding rule (halves up), or the figure jumps a pixel when one set follows another.

## 6. One ground line before anything is shown (`lineup`)

`pixel_look.py lineup --master <master> --item <a sprite or a set folder> --item ... --output-dir <new>` puts the master, the sprites made from it and every drawing of the sets on one ground line (by the middle of their feet), with lines at the master's top and neck, and prints the heights (the body's: a narrow column over the feet, so that a staff carried beside the body does not count). Run it before a motion is shown to the user: **a change of build between two sets is a blocker**, not a detail. The first drawing of every item is compared with the master (`verdict`); a crouch or a blade over the head is a pose, look at the picture.

## Records

`<name>.json` (`generate2dsprite.pixel_look.v2`): `size`, `colours`, `palette`, `feet` and `anchor` (the root for the game), what was read (`read`, `lattice`, `taken`, `ownLattice`), the source picture's hash. `request.json` (`generate2dsprite.pixel_look_request.v1`) carries the lattice of a reference. `motion.json` and `package/animation.json` are those of every pixel_motion set; `measure.json` is `video2dsprite.pixel_walk.v1`.

## Limits

- A master is a little simpler than its picture (above). A clean-up redraw from the master is not tried yet.
- `measure` reads the feet in the ground rows around the sprite's own feet. A pole on the ground is left out and a dragging blade is worked around, but `strideCells` still counts what drags behind, and thin bare legs can be taken for poles. It cannot see which leg is in front: that is what `legs.png` is for.
- `breath` moves whole cells: wings, tails and cloth do not flutter. Use a clip where they must.
- A picture with cells under 8 px is refused as a master; a clip whose sprite pixels are under 5 video px blurs (`prepare` warns).
- A clip whose first frame is not the sprite (`firstFrameDiffers` is large) was resized or restyled by the provider: generate again. (A picture that only lies a few px beside the job's place, to either side, is found: the first frame decides on which cells the sprite lies.)
- A large sprite (about 80 x 110 and more) with the room of a wide swing fits a 720p clip only at 4 px a sprite pixel. It still reads on the grid, but the shades of a dark figure flip between near colours (about an eighth of the first frame's cells against a fifteenth at 5 px), and in the fastest frames of a swing a thin dark blade takes the key colour: look at the frames sheet and leave those frames out of the picks.
- Finish a sprite before a clip is made from it. A clip is read against the sprite it started from; read against a sprite that was made again afterwards (even with a palette a few steps apart), about a tenth of the first frame's cells differ: generate the clip again from the final sprite.
- A video model's attack often turns the whole body, and its recovery can jump back to the stance: name the strike plainly, pick the drawings, and set the game's timing with the durations, not the clip's.
- Effects (slashes, sparks, magic) are not part of the body clip: add them in the game, or as FX sheets.
