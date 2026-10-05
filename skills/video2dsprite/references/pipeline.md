# Processing and runtime contract

## Inputs and CLI

The processor has no network calls. Generation may use native tools, the sibling
`generate2dmedia` API adapter, or an existing video. Keep original art/video and
generation provenance outside generated asset folders.

```bash
python skills/video2dsprite/scripts/video2dsprite.py extract --video motion.mp4 --out-dir raw --start 1 --duration 2 --fps 12
python skills/video2dsprite/scripts/video2dsprite.py clean --raw-dir raw --out-dir clean --key-mode magenta
python skills/video2dsprite/scripts/video2dsprite.py sample --clean-dir clean --out-dir comparison --frame-counts 12,24 --playback-duration 2
python skills/video2dsprite/scripts/video2dsprite.py package --clean-dir clean --out-dir game-assets/idle --name idle --fps 12 --source-size 448,448 --source-anchor 224,430 --max-side 320 --formats png,webm,packed --loop
```

For native transparent frames `clean --key-mode none` copies alpha exactly.
`auto` preserves alpha if present; otherwise it keys magenta. Do not key a colorful
opaque scene. Magenta flood fill removes connected background and preserves enclosed
magenta clothing. Key color touching clothing or trapped background holes still
needs a mask/manual review; this is not semantic segmentation.
Optional `--despill 0..1` removes a bounded amount of magenta contamination from
the first visible boundary's RGB without altering alpha or interior colors. Default
is 0 because violet costume edges may be intentional. Native-alpha frames in `auto`
and all frames in `none` bypass both keying and despill.

`extract --decoder libvpx-vp9` explicitly preserves VP9 WebM alpha. This is unnecessary
for ordinary opaque MP4. `--fps 0` extracts original frames; for variable frame-rate
sources, resample to a chosen constant fps before packaging. Package fps is explicit
and must match the decoded interval's intended playback speed.

`sample` is a comparison tool: evenly spaced indices include first/last, and fewer
available frames are reported honestly instead of fabricating frames. The old GIF
tempo remains when `--playback-duration` is omitted and is labelled preview-only.
Do not infer gameplay duration from it. `process` combines extraction, cleaning and
sampling; it does not automatically certify or loop-trim a generated video.

## Fixed geometry

All frames must share the same source canvas. Default `package` retains it. Optional
`--crop-union` calculates a single envelope for the complete clip. One resize applies
to every frame; there is no frame-dependent recentering or scale normalization.
Even dimensions pad right/bottom transparently for codec compatibility.

For world anchor `(x,y)` and scale `s`, draw `contentSize` pixels from the media to:

```text
left   = x + (sourceRect.x - sourceAnchor.x) * s
top    = y + (sourceRect.y - sourceAnchor.y) * s
width  = sourceRect.width * s
height = sourceRect.height * s
```

`sourceSize` and `sourceAnchor` describe approved art units, even when the encoding
is much smaller. `sourceRect` is `[x,y,width,height]` in those same units.
`encodedSize` includes even padding; `contentSize` excludes padding. `encodedAnchor`
is a derived media coordinate, not a replacement for world geometry.

Example: source art 448×448, anchor `[224,430]`, mobile media 320×320. The source
geometry stays 448×448 / `[224,430]`. Changing it to 320×320 without adjusting world
geometry makes actor size/contact differ across devices. Collision and hitboxes
are gameplay data; don't derive them from the changing animation alpha.

## Outputs

```text
animation.json             geometry, timing, file hashes, formats, input provenance
animation-qa.json          seam differences, bounds drift, empty/edge frames
idle-poster.png            first decoded frame, same geometry as runtime media
idle-atlas-00.png ...      PNG fallback pages (at most 4096×4096)
idle.webm                  optional VP9 alpha (libvpx-vp9)
idle-packed.mp4            optional RGB-left / alpha-right H.264 (libx264)
```

PNG fallback is always generated, independent of requested video formats.
Each atlas page records `firstFrame`, `frameCount`, `columns`; cell size is fixed.
Read files through the current manifest, not directory globbing across old runs.
`inputFrames` records source names, SHA256 and byte size. Requested encoders must
exist before output is written. Encoded streams undergo a complete decode check;
this does not replace real browser/alpha or performance review.
`package` requires a new destination. It writes into a temporary sibling directory
and publishes that directory only after the encoders/decoder checks complete; an
encoder failure leaves no partial accepted manifest or mixed-version assets.

The processor retains frames in memory, with a 128-million decoded-pixel cap for
`package`. Long/high-resolution clips should be trimmed or decoded at lower fps.
Typical starter budgets are 12fps props and 24fps actors at 256–384px per side, then
measure. Do not preload an entire game's videos or assume compressed bytes predict
RAM, CPU or GPU use.

## Transparent playback

VP9 WebM may play while ignoring alpha on some browser/device combinations. Packed
MP4 avoids depending on native alpha, but needs a compositor. The bundled
[packed-alpha-runtime.js](packed-alpha-runtime.js) demonstrates reconstruction:

```js
import { createPackedAlphaDrawable, drawAnchoredFrame } from './packed-alpha-runtime.js';
const video = document.createElement('video');
video.muted = true;
video.playsInline = true;
video.loop = clip.loop;
// For a cross-origin asset, set crossOrigin before src and configure server CORS.
video.src = assetBase + clip.packedAlpha.file;
const output = createPackedAlphaDrawable(video, clip.packedAlpha);
// Call within the application's visible-frame loop, after a user gesture if required:
if (output.update()) textureNeedsUpdate = true;
// Draw the poster until update() succeeds at least once. Handle play() rejection.
drawAnchoredFrame(ctx, output.drawable, clip, actor.x, actor.y, actor.scale);
```

The helper is a correctness/fallback example using CPU Canvas2D readback, not a
high-throughput renderer. At larger counts use a WebGL shader that samples the
left half as RGB and right half's red channel as alpha, preserving the same geometry
and straight-alpha blending. Use `requestVideoFrameCallback` where available, share
one decode for identical clips, and stop hidden/offscene media. Set an active decoder
budget and prioritize controllable actors; distant props may use posters. Pick the
budget from device measurements rather than assuming a universal safe count.
The application owns `play()`, pause, seek, listeners, unloading
(`removeAttribute('src'); load()`), and resource scheduling. Avoid `file://`, verify
media headers/range behavior and CORS, and profile real devices. A pack alone is not
an iPhone FPS guarantee.

## QA interpretation

`seamPremultipliedMAE` compares first/last premultiplied RGBA on a small sample.
`seamToMedianRatio` relates that to ordinary adjacent changes; neither is a pass
threshold. Transparent hidden RGB is excluded. Bounds-bottom/center spans reveal
possible root drift but also respond to intended jumps or trailing cloth. Inspect
the actual contact point and identity across the full clip.

Game code decides when an attack lands. `hitEvents` starts empty; populate reviewed
game timing separately. Never trigger damage based only on video `ended`, decoded
frame counts, or a provider's assumed action timing.
