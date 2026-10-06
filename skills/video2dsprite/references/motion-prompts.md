# Motion prompts for sprite sets

`scripts/sprite_set.py` builds every image-to-video prompt from this file. `plan` copies the
templates, the clause library and the gate map into `set_plan.json`, so a set keeps the wording it
was planned with even when this file changes later. Edit an action's `motion` or `negatives` in
`set_plan.json` to change one set; edit this file to change future sets.

The fenced blocks are machine-read: ` ```template <action>`, ` ```negatives <action>`,
` ```clauses` (`id | text`) and ` ```gatefix` (`gate | clause, clause`). Everything else is prose.
Placeholders: `{facing}` (LEFT, RIGHT, the viewer, away from the viewer), `{toward}` and `{away}`
(the facing side and the opposite side), `{pos}` (the possessive from `plan --pronoun`, default
their), `{key}` (magenta), `{action}` and `{style}`.

## Prompt shape

The shape that worked on 121 Grok takes of the game-opus55 sprites (`tools/run_c.sh`,
`art/logs/grok_vs_*.txt`):

    The same <identity_recap>, facing <FACING> the entire time and never turning around,
    <ONE action from the template>. The clip starts AND ends in exactly the same pose as the
    still. <action negatives> <fix clauses from failed takes> camera locked, no zoom, no pan;
    solid flat pure magenta background only for the whole shot; keep the exact <style> look,
    palette, proportions and costume of the still; single continuous action.

Rules behind it:

- Repeat the long identity recap from `master.json` in every prompt. Short recaps ("the same
  pixel-art boy") let the model redesign the character between actions.
- One action per clip. A combo is two clips.
- State the facing as a lock, not a description. Turning around was the most common rejection.
- Pin the start and the end pose to the still. Idle and attack also get the still as the last
  frame when the route takes one (`--last-frame`).
- Name the backdrop and the style last, every time. The model forgets them otherwise.
- Never ask for a numeric pixel grid, a work region or slow motion; the input image already
  frames the shot, and weak words ("tiny", "barely", "very slow") make the motion invisible at
  game size (prepare_i2v_input lint).
- Keep effects out of character clips. Dust, sparks, trails and flashes cover the body, fail the
  key and belong to the game's own FX layer.
- A fix is a sentence added to the next take, never a rewrite of the whole prompt.

## Action templates

The template continues the sentence that starts "The same <identity>, facing <FACING> the entire
time and never turning around,". Write it as a verb phrase without a subject pronoun.

### idle (loop, square canvas, end frame pinned)

```template idle
stands in place in {pos} ready stance as a looping game idle animation: slow, clearly visible breathing with the chest and shoulders rising and falling, a slight bob in the knees, hair, cloth and loose parts stirring gently in a soft wind. No steps, no attack, no gestures. The feet stay planted on the same ground line and the body stays centered in the frame.
```

```negatives idle
Nothing appears around the character: no particles, no smoke, no dust, no effects.
```

Floating classes (spirit, ghost, hover, flying) use this idle instead:

```template idle-hover
floats in place as a looping game idle animation: a subtle, slow hovering bob of a few pixels, slow breathing, hair, cloth and the spectral lower body stirring and curling gently. No travel, no attack, no gestures. The body stays centered in the frame at the same height and the same size.
```

### walk (loop, square canvas)

```template walk
walks in place like a looping game walk cycle on a treadmill: clearly alternating left and right steps with heel contact, a bent-knee passing pose and toe-off, arms swinging opposite to the legs, a small natural up-and-down weight shift. The character stays in the same spot and never moves across the frame; one foot is always on the same ground line.
```

```negatives walk
Constant character size. No dust clouds, no speed lines, no effects.
```

### run (loop, square canvas)

```template run
runs in place like a looping game run cycle: a fast, determined sprint with the upper body leaning forward, knees pumping, feet pounding with a brief flight phase, arms pumping opposite to the legs, hair and clothing streaming back. The character stays in the same spot and never moves across the frame; the feet land on the same ground line.
```

```negatives run
Constant character size. No dust, no speed lines, no motion trails, no effects.
```

### attack (one-shot, 16:9 canvas, end frame pinned, hit tick)

```template attack
starts in {pos} ready stance, draws back a little (a short wind-up), then performs ONE fast, powerful attack {toward} with {pos} weapon or fists at chest height, holds the strike for a brief moment, then returns to the exact same ready stance as the start and holds still. Only one attack: no second strike, no spinning, no jumping, no leaving the frame. The back foot stays planted.
```

```negatives attack
The weapon stays one single weapon of the same length and shape. Constant character size. No motion trails, no slash effects, no sparks, no effects.
```

### jump (one-shot, 3:4 canvas with 34% headroom)

```template jump
dips into a quick short crouch, then springs straight up with the knees tucked, rises in one smooth arc, stays at the peak only for a brief instant, falls back down at a natural speed and lands on exactly the same spot, absorbs the landing with a short crouch, then rises back into the exact same standing pose as the start and holds still. One continuous, fluid jump with no pauses and no hovering; the whole body stays inside the frame.
```

```negatives jump
Constant character size. No dust, no shockwave, no motion trails, no effects.
```

### hurt (one-shot, square canvas)

```template hurt
holds {pos} ready stance for a brief moment, then is knocked back a little by an unseen blow from the front: the whole body jolts backward {away} by a few pixels, the head tilts back and the shoulders jerk up for a split second, then the character sinks back into the exact same stance and holds still. A small, quick recoil, under one second; the feet stay planted and the character keeps facing {facing}.
```

```negatives hurt
No projectiles, no flashes, no sparks, no blood, no effects.
```

### cast (one-shot, 16:9 canvas)

```template cast
keeps {pos} stance and raises one hand forward {toward} in a calm commanding casting gesture, holds the pose for about one second while hair and clothing sway, then lowers the hand smoothly and returns to exactly the same pose as the start, then holds still.
```

```negatives cast
The game adds the magic: no glow, no runes, no energy in the hands, no new objects, no effects.
```

### guard (one-shot, square canvas)

```template guard
braces into a compact defensive stance with bent knees and the arms, or the existing weapon or shield, raised in front of the chest, holds the guard steady for about one second with a small recoil as if absorbing a heavy blow, then relaxes back into exactly the same pose as the start and holds still. No attack, no steps.
```

```negatives guard
No new shield or weapon, no sparks, no effects.
```

### victory (one-shot that ends in a held pose, 3:4 canvas)

```template victory
celebrates with one confident gesture, such as a raised fist or the weapon lifted high, then holds a proud victory pose with relaxed breathing while hair and clothing settle. No jumping, no spinning, nothing leaves the frame.
```

```negatives victory
No new objects, no confetti, no effects.
```

### defeat (one-shot that ends kneeling, 16:9 canvas)

```template defeat
loses strength: the shoulders sag, the head bows, and the character sinks heavily onto one knee with one hand pressed to the ground or resting on the weapon, then stays kneeling with only slow, heavy breathing for the rest of the clip. Stays in the same spot; never falls flat and never stands up again.
```

```negatives defeat
No dissolving body, no fragments, no ghosts, no blood, no effects.
```

## Failure-to-negative clause library

Each clause is the wording that fixed (or was written to fix) a rejected game-opus55 take. The
sources are `art/logs/_rejected_keiji.txt`, `art/logs/_kanetsugu_takes.txt` and the `_r2`/`_r3`
retakes in `art/logs/grok_vs_*.txt`:

| Failure seen | Example take | Clause |
|---|---|---|
| turned his back to the camera, face gone for a second | v_ult_keiji r6a, r8a | never-turn |
| came back 12-40% bigger after the action (push-in) | keiji r1, r2a, r4a | camera-locked |
| grew legs and an armour skirt; longer limbs | v_ult_kanetsugu take 4 | constant-size |
| horns, antlers and a tall crest appeared on a plain helmet | akazonae hurt r1 (fixed in r2/r3) | identity-lock |
| hair turned orange-brown mid-clip | keiji r4a | identity-lock, keep-colours |
| fire band, arm or spinning spear across the face | keiji r3b, r5b, r7b; kanetsugu take 4 | face-visible |
| double-bladed spear, ghost blade tips, the spear bends | keiji r2b, r3b, r6b, r8b | weapon-shape |
| dust, pebbles and puffs at the feet; sparkle at the spear tip | ashigaru atk r1, run r1 (fixed in r2) | no-extras |
| particles, orbs, arrows and spears flying past | kage hurt r1, r2 (fixed in r3) | no-extras |
| garbled Latin-like script on the letter | v_ult_kanetsugu take 1 | no-text |
| huge spiky starburst, four-point sparkles | keiji r5a; kanetsugu kv takes | no-flares |
| stood up straight, raised the katana over the head in a hurt | akazonae hurt r2, r3 | single-action, same-start-end |
| feet stamped and stepped during a planted thrust | ashigaru atk r3 | feet-planted |
| framing changed, the spear tip cut by the top edge | keiji r3a | inside-frame, camera-locked |
| frozen or hovering mid-jump | homura jump | fluid-motion |
| a run that drifted got "feet stay planted" and lost its stride | Aria run take 4, 2026-10-06 | feet-ground-line (never feet-planted for walk, run or jump) |
| boots olive in one frame, maroon in the next; a pink hand | Aria run takes 1-9, 2026-10-06 | keep-colours (and the finish's colour lock) |
| the attack thrust held 1.7 s, the jump 5.3 s: slow motion | Aria attack, jump, hurt, 2026-10-06 | fluid-motion (and retime --auto-oneshot) |

```clauses
never-turn | The character keeps facing {facing} the whole time: never turns around, never shows {pos} back and never faces the camera.
camera-locked | Locked camera and constant character size: no zoom, no push-in, no pull-out, no pan; the character is exactly the same size in every frame, before and after the action.
constant-size | The character keeps exactly the same size and proportions as the still in every frame; arms, legs and the weapon never grow longer.
feet-planted | Both feet stay planted on the same ground line and the body stays centered in the frame; no stepping away, no sliding, no drifting.
feet-ground-line | Every step lands on the same ground line and the body stays centered in the frame; the character never drifts forward, backward, up or down.
inside-frame | The whole body, the weapon and every moving part stay fully inside the frame with a clear margin on every side; nothing is cut off by the frame edge.
flat-background | The background stays solid flat pure {key} for the whole shot: no floor, no ground, no shadow, no gradient, no glow, no haze, no light.
no-extras | Nothing else appears in the frame: no dust, no dirt, no smoke, no puffs, no pebbles, no particles, no orbs, no sparks, no flashes, no projectiles, no arrows, no motion trails, no effects of any kind.
identity-lock | CHARACTER LOCK for the whole clip: the face, hair, colours, costume, proportions and every design detail stay exactly as in the still; nothing new grows or appears on the character (no horns, no crests, no extra straps or weapons).
face-visible | The whole face is visible in every frame: no hand, weapon, sleeve, hair or effect ever passes in front of the face.
keep-colours | The colours stay exactly as in the still in every frame: no tint, no colour shift, no darkening and no glow on the character.
same-start-end | The clip starts AND ends in exactly the same pose as the still and holds that pose still at the end.
starts-from-still | The first frame is exactly the supplied image: the same pose, position and size; the motion starts from there.
single-action | Only one {action}: no repeats, no second strike, no spinning, no jumping, no leaving the frame.
weapon-shape | The weapon stays one single weapon of the same length and shape the whole time: never bending, never doubling, no second blade, no ghost blades.
no-text | No text, letters, glyphs, writing or logos anywhere.
no-flares | No cross flares, no starbursts, no four-point sparkles, no lens flares.
visible-motion | The motion is clearly visible at game size: a full, readable {action} with real movement of the body, not a still image.
in-place | The character stays in the same spot and never travels across the frame; the game moves the sprite.
fluid-motion | One continuous, fluid motion at natural speed: no pauses, no frozen poses, no hovering, no slow motion.
steady-cycle | A steady, evenly timed cycle: every step looks the same, the legs clearly alternate and the pose at the end of each cycle matches its start.
no-blur | No motion-blur smears, no afterimages, no colour fringing on the weapon.
```

## Gate to fix map

`run` adds these clauses to the next take when a numeric gate fails (each clause once per
action). The semantic failures that no number can see (weapon shape, text, flares, a weak action)
are added by the agent after review: `sprite_set.py retake --action attack --fix weapon-shape`.

Feet: `feet-planted` is for actions that stand (idle, attack, hurt, cast). A walk, run or jump
whose feet gate fails gets `feet-ground-line` instead, also when the agent asks for
`feet-planted` with `retake`, and a planted-feet clause left in an older set's state is dropped
from those prompts: telling a run to keep both feet planted freezes its stride. The colour gate
(key bleed, hues the master lacks) adds `keep-colours`; the timing gate adds `fluid-motion` only
when the set keeps the clip's speed (`"mode": "source"`), since one-shots are otherwise retimed to
game length after generation.

```gatefix
area | constant-size, camera-locked
feet | feet-planted, in-place
identity | identity-lock, face-visible
zoom | camera-locked
turn | never-turn
edge | inside-frame
background | flat-background
extra | no-extras
motion | visible-motion, single-action
end-pose | same-start-end
colour | keep-colours
timing | fluid-motion
loop | steady-cycle, in-place
registration | starts-from-still, camera-locked
keying | flat-background, inside-frame
```

## Writing a fix by hand

- Name what must stay, then what must not appear: "The katana stays low, in both hands, pointing
  forward the whole time: he never lifts it, never raises it above his shoulders" (akazonae hurt
  r3) beat "don't swing the katana".
- Quote the design from the still when it drifts: "a plain rounded red helmet with only the small
  gold four-diamond crest; no horns ever appear".
- Move effects behind or below the character instead of banning the action: "the fire walls rise
  BEHIND him", "the flames stay below his chest".
- After two failed fixes, change the action itself (a smaller recoil, a level spear) rather than
  piling more negatives on the same motion.
