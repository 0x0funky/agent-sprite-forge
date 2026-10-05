# Engine export and runtime playback

`scripts/export_engine.py` turns built clips (`animation-clips.json` from `build_animation_clips.py`, schema v1 or v2) into engine files: an Aseprite-style JSON atlas, Godot 4 SpriteFrames, and a Godot Sprite3D package. It never changes pixels, timing or the anchor; it only lays them out for each engine and proves the layout by reading it back.

> **Not verified in editors.** These exports have not been imported into Aseprite, Godot 4, Phaser, PixiJS or Unity by this skill. `export_engine.py` proves parse-level round trips only (see QA below). Import once by hand in each target engine and check the anchor, timing and loops before calling a pipeline compatible.

## Commands

Run from the project root. Outputs go to a new directory inside the project; an existing directory is refused.

```bash
python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target all --output-dir out/hero-engine
python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target aseprite-json --output-dir out/hero-aseprite --sampling nearest
python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-spriteframes --output-dir out/hero-godot --godot-fps 12
python "<skill-dir>/scripts/export_engine.py" --clips out/hero-clips/animation-clips.json --target godot-sprite3d --output-dir out/hero-3d --world-height 1.7 --billboard fixed-y
```

In Claude Code, `<skill-dir>` is `${CLAUDE_SKILL_DIR}`. On success the tool prints one JSON line with `status`, `output`, `metadata` (the `engine-export.json` path), `targets`, `clips`, `frames` and `pages`. Errors print `error: ...` and exit 1, and nothing is published.

Export the **built** manifest. A clips input manifest (`clips.json`) is refused with a pointer to `build_animation_clips.py`, which resolves names, ticks and pingpong into explicit frames and integer milliseconds first.

| Option | Default | Meaning |
|---|---|---|
| `--target` | `all` | `aseprite-json`, `godot-spriteframes`, `godot-sprite3d` or `all` |
| `--name` | output folder name | base name of the files (letters, digits, `-`, `_`) |
| `--max-atlas-size` | 4096 | largest atlas side; whole clips move to extra pages |
| `--padding` / `--extrude` | 2 / 1 | transparent gap between cells / replicated edge pixels around each frame |
| `--sampling` | `auto` | `nearest` or `linear`; auto reads the manifest `sampling` or `pixel_art`, else linear |
| `--godot-fps` | `auto` | Godot animation speed; auto uses a clip's ticks when it has them, else its shortest frame |
| `--world-height` / `--pixel-size` | pixel size 0.01 | Sprite3D scale: Godot units for the subject height, or the pixel size itself |
| `--subject-height-px`, `--reference-clip` | measured | subject height used for `--world-height` (else `body_height_px`, else the idle clip's median visible height) |
| `--billboard` | `enabled` | Sprite3D billboard: `enabled`, `fixed-y` or `disabled` |
| `--ppu`, `--camera-pitch-deg` | 1 / pixel size, none | fill the Unity and pitch-compensation rows of the mapping table |

## What each target writes

| Target | Files | Notes |
|---|---|---|
| `aseprite-json` | `aseprite/<name>.json`, `aseprite/<name>.png` | JSON array atlas. Frames are named `"0"`, `"1"`, ... (Aseprite's `{frame}` naming, which Phaser's `createFromAseprite` expects). One `frameTags` entry per clip (`direction` forward, `repeat` `"1"` for one-shot clips), tag `data` holds the events as JSON, one `anchor` slice carries the pivot, and a top-level `animations` map lists each clip's frame names for PixiJS. |
| `godot-spriteframes` | `godot/<name>.tres`, `godot/<name>.tscn`, `godot/<name>-atlas-NN.png` | SpriteFrames over AtlasTexture regions (`filter_clip` on) and an AnimatedSprite2D scene with the anchor offset and texture filter. |
| `godot-sprite3d` | `godot-sprite3d/<name>.tres`, `.tscn`, `frames/*.png`, `action-<clip>.json`, `bundle.json` | AnimatedSprite3D over one texture per frame (no atlas, so mipmaps cannot bleed between cells), plus `generate2dsprite.godot_sprite3d.v1` contracts and a bundle whose paths are all relative. |
| always | `engine-export.json` | the files with sha256, per-clip frames, durations, events, transition hints, keys and Godot timing, the mapping table and the QA envelope |

Atlas pages are at most 4096 px. Identical frames share one cell. A clip never spans two pages (an Aseprite tag addresses one image), so a clip whose distinct frames do not fit one page is refused: split it or export smaller frames. Frames are not trimmed or rotated.

Text paths inside the export are relative: Godot resolves a relative `ext_resource` path against the `.tres`/`.tscn` that names it (Godot rewrites it to `res://` when it saves the resource again), and every Sprite3D contract lists its frames relative to itself (finding S19). Move the whole output folder into the engine project; nothing points back at the source bundle.

### Godot timing

Godot shows a frame for `relative_duration / speed` seconds, so the export writes `relative_duration = ms x speed / 1000`:

- `--godot-fps N`: speed N for every clip.
- auto, a clip with integer `ticks` at `tick_hz` (consistent with its ms): speed = tick rate, relative durations = ticks (exact 60 Hz timing).
- auto, otherwise: speed = 1000 / shortest frame ms, so the shortest frame is 1.0 and an evenly timed clip is all 1.0.

The round trip requires the recovered milliseconds to equal the source to 1e-6 ms (1 ms for tick-timed clips, the builder's tick rounding). SpriteFrames cannot carry events: read them from `engine-export.json` (`clips.<name>.events_ms`, with the frame position `at`) or the Aseprite tag data.

## Anchor mapping table

The clips share one anchor `anchor_px = (ax, ay)` in a `w x h` frame (continuous pixel coordinates, y down; often the ground contact). `engine-export.json` carries these values for the exported sprite under `mapping`.

| Engine | Setting | Value |
|---|---|---|
| Godot AnimatedSprite2D | `centered = false`, `offset` | `(-ax, -ay)` (written to the `.tscn`) |
| Godot AnimatedSprite2D | `centered = true`, `offset` | `(w/2 - ax, h/2 - ay)` |
| Godot Sprite3D / AnimatedSprite3D | `centered = true`, `offset` (+Y up) | `(w/2 - ax, ay - h/2)` |
| Godot Sprite3D | `pixel_size` | `world_height / subject_height_px`; Godot's default is 0.01 |
| Unity | Sprite pivot (normalized, origin bottom-left) | `(ax / w, 1 - ay / h)` |
| Unity | Pixels Per Unit | `1 / pixel_size` (100 at Godot's default); pixel art often uses its tile size |
| Phaser | `sprite.setOrigin(x, y)` | `(ax / w, ay / h)` |
| PixiJS | `sprite.anchor.set(x, y)` | `(ax / w, ay / h)` |
| Aseprite | slice `anchor` pivot | `(round(ax), round(ay))` half up; the exact value is in the slice `data` |
| Yaw-only (fixed-y) billboard under a camera pitched by p | vertical scale about the anchor | `1 / cos(p)` (`--camera-pitch-deg` fills an example) |

Texture filter: `nearest` for pixel art, `linear` for painted art.

| Engine | nearest | linear |
|---|---|---|
| Godot 2D (`texture_filter`) | 1 | 2 |
| Godot 3D (`texture_filter`) | 0 | 3 (linear with mipmaps; per-frame textures, so no cell bleeding) |
| Unity | Filter Mode Point, Compression None | Bilinear |
| Phaser | game config `pixelArt: true` | default |
| PixiJS | `scaleMode` nearest | linear |

## Manual import checklist

- **Godot 4**: copy `godot/` or `godot-sprite3d/` into the project and open the `.tscn`. For pixel art set the PNG import to Lossless without mipmaps (Godot may switch textures used in 3D to VRAM compression, which blurs pixels). Check the feet on the node origin and `autoplay`.
- **Phaser**: `this.load.aseprite('hero', 'hero.png', 'hero.json')`, then `this.anims.createFromAseprite('hero')` and `sprite.setOrigin(...)` from the table. Check that one-shot clips stop and per-frame durations hold.
- **PixiJS**: load `hero.json`; `sheet.animations.<clip>` lists the textures; give an AnimatedSprite per-frame `time` from `frames[i].duration`.
- **Unity**: import the PNG as a Sprite (Multiple) with an Aseprite-JSON importer or an editor script that reads `frames[].frame`; set pivot and Pixels Per Unit from the table.

Record what you imported and in which engine version before claiming support in a README.

## Runtime playback

The video2dsprite skill ships DOM-free runtime helpers in its `references/runtime/forge-runtime.mjs` (with the packed-alpha WebGL compositor beside it). They read `engine-export.json` clips, `animation-clips.json` clips and video `animation.json` 2.0/3.0 alike (`normalizeClip`), and work for sprite sheets too.

- **Distance-driven walk.** Drive the gait phase from distance actually travelled, never from the wall clock: feed the position after collision to `TravelMeter`, then `walkPlayback(clip, meter.distance, speed)` or `gaitFrame(distance, stride, frames)`. Both share one phase, so a blocked actor keeps its pose and a video walk and a sheet walk stay in step. The travel per loop is `stride_world_units` (sprite clips, per clip cycle) or `strideWorldUnits x cycles` (video). Enter on `entry_frame` with `phaseOffset(distance, loopDistance, entryPhase(clip))`.
- **Transitions.** `transitionHint(clip, to)` gives the entry frame and dissolve time from the clip's hints. `ditherDissolve(progress, x, y)` keeps the outgoing pose's pixel at sprite-local `(x, y)` while true; draw the incoming pose underneath (`ditherDissolvePixels` does this on pixel arrays). Mode `premultiplied` uses `premultipliedMix`.
- **Hit-stop.** One `HitStopClock` per entity: `hitStop(clip.hitstop_ticks)` at the hit event freezes the action clock while the world clock keeps shaking and spawning particles. Longer requests win; they never stack.
- **Time-warp onto hits.** `mapActionTime(clip, {durationMs, impactTimesMs}, elapsedMs)` stretches the clip so its impact frame lands on every gameplay hit (multi-hit attacks included) and returns the frame and playback rate.
- **Fixed step and events.** `FixedStepLoop` runs the simulation at 60 Hz with an interpolation `alpha` for rendering and drops a long stall instead of spiralling; `frameAt(clip, t)` picks frames from integer durations that sum exactly; `eventsCrossed(clip, previousMs, nowMs)` fires each event once per crossing.
- **Integer-scale snapping.** For pixel art pick `integerScale(available, frameHeight)` and place frames with `anchoredRect(source, x, y, scale, {snap: true})`; never draw at fractional scales.
- **Sampling.** Use nearest sampling for pixel art (`texture_filter`, `imageSmoothingEnabled = false`, Phaser `pixelArt`) and linear for painted art, as recorded in `engine-export.json` `sampling`.

Pitfalls seen in earlier prototypes: choosing walk frames from elapsed time (feet slide, and the walk keeps playing against a wall); drawing pixel art at fractional sizes (uneven pixel widths); hard-coded foot offsets instead of the anchor; a sine bob added on top of drawn walk frames; splitting a sheet by a guessed frame count instead of the manifest.

## QA and what it does not prove

Before publishing, every written file is parsed back: Aseprite frames are cropped from the atlas and compared with the source frames (exact RGBA), durations, tags, events and the pivot are compared, the SpriteFrames `.tres` and `.tscn` are parsed and their textures, regions, loops and recovered milliseconds checked, and every Sprite3D frame path is resolved relative to its contract with its sha256. Any mismatch publishes nothing. `engine-export.json` records the checks in a QA envelope (`method`, `notProven`, inputs and outputs with sha256). A fractional anchor gives status `warn` (the Aseprite pivot is rounded).

Not proven: imports and playback in Aseprite, Godot, Phaser, PixiJS and Unity; engine import settings (filter, compression, mipmaps are not written); animation quality and timing feel.
