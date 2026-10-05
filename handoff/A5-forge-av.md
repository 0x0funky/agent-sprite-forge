# A5-forge-av handoff

Canonical library: [shared/forge_av.py](../shared/forge_av.py) (`FORGE_AV_API_VERSION = "1"`).
Byte-identical copies: [skills/video2dsprite/scripts/forge_av.py](../skills/video2dsprite/scripts/forge_av.py),
[skills/generate2dmap/scripts/forge_av.py](../skills/generate2dmap/scripts/forge_av.py).
Tests: [tests/test_forge_av.py](../tests/test_forge_av.py) (30 tests; 22 carry the `ffmpeg` marker and skip
when ffmpeg/ffprobe are missing or fail the functional probe).

Every Appendix A function exists with the frozen name and positional parameters. Additions are
keyword-only or new names, so nothing frozen changes.

## 1. CLIs

None. forge_av is a library: it has no command line and prints nothing. Skills import their local copy
(`sys.path.insert(0, str(Path(__file__).parent))`, then `import forge_av`). One-line uses:

```text
info = forge_av.ffmpeg_info()        # info["functional"]["vp9_alpha"], ["libx264"], info["errors"], info["version"]
meta = forge_av.probe("clip.mp4")    # width, height, fps_rational, nb_frames, duration, vp9_alpha, audio_streams, attached_pics
paths = forge_av.extract_frames("clip.webm", "work/frames-raw", start=1.0, duration=2.0)   # frame_000000.png ...
frames = forge_av.decode_rgba("idle.webm")                                                # libvpx-vp9 keeps alpha
rgba = [forge_av.unpack_packed_alpha(f, packed) for f in forge_av.iter_rgba("idle-packed.mp4")]
webm = forge_av.encode_vp9_alpha("work/clean", "out/idle.webm", "24/1")
packed = forge_av.encode_packed_alpha(frames, "out/idle-packed.mp4", 24, keyint=len(frames))
loop = forge_av.encode_h264_loop(frames, "out/loop.mp4", "24/1", crf=14)
ok = forge_av.moov_before_mdat(p) and forge_av.timestamps_increasing(p); n, ms = forge_av.packet_count(p), forge_av.duration_ms(p)
```

API notes for adopters (B05, B08, B16, B22):

- `ffmpeg_info()` proves capabilities by running forge_av's own encoders. `vp9_alpha` means a
  half-transparent VP9 frame survives `encode_vp9_alpha` and a libvpx-vp9 decode. `libx264` means a
  packed H.264 encode decodes again. The result is cached per (ffmpeg, ffprobe) path pair and returned
  as a copy. The extra key `errors` explains each failed check. ffmpeg older than 5.1 fails the probe
  because `-fps_mode` is missing.
- `probe(path)` also returns `path`, `format`, `stream_index`, `nb_frames_source`
  (`container`|`packets`), `rotation`, `alpha_mode` (WebM AlphaMode, VP8 or VP9), `has_alpha`, `audio`
  (per-track codec/channels/sample_rate/duration) and `attached_pics`. `width`/`height` are display
  size after rotation, i.e. what the decoders return. `duration` (seconds) is the video stream's, never
  the longer audio. Attached cover art is never chosen as the clip, and decoders `-map` the probed stream.
- `alpha` for `extract_frames`/`iter_rgba`/`decode_rgba`:
  - `auto` (default) keeps alpha when the stream has it, decoding VP8/VP9 WebM alpha with libvpx.
  - `on` raises ValueError for an opaque source.
  - `off` returns opaque frames.
- `start`/`duration` select frames timed in [start, start + duration), using an input-side accurate seek.
- `extract_frames` writes `frame_000000.png` and onwards (0-based frame index, 6 digits): RGBA when alpha
  is kept, else RGB. It refuses an `out_dir` that already holds `frame_*.png` and removes its own frames
  on failure. A `%` in the directory is escaped.
- `iter_rgba(...)` (new) streams writable frames one at a time and kills ffmpeg when the loop is left.
  `decode_rgba(..., max_pixels=2**28)` raises ValueError beyond the budget. Both have `size=(w, h)`:
  resized in ffmpeg with `premultiplied_scale_filter` when alpha is kept.
- Encoders take a PNG directory (natural sort) or a sequence of uint8 arrays, PIL images or image paths,
  all one size.
  - There is deliberately no encoder-side resize. Resample first with `forge_core.resample_rgba`
    (anchor-pinned), because the edge bleed must run after resizing.
  - Colour under alpha 0 is zeroed, then bled 3 px from visible neighbours (`EDGE_BLEED_PX`). Alpha is
    never changed.
  - Output bytes are deterministic for a given ffmpeg/x264/libvpx build (bitexact muxing, fixed threads).
  - An existing `out` is never replaced; a hidden `.partial` sibling is published by hard link (or an
    exclusive-create copy).
  - Packet count, and keyframe placement when `keyint` is set, are verified before publishing.
- Keyword-only additions on all three encoders:
  - `max_fps` (default and ceiling 60; lower for tiers). Frames above it keep the nearest frame per tick;
    the result records `fpsCapped`, `inputFps`, `inputFrameCount` and `inputIndices`.
  - `timeout`.
  - `keyint` on `encode_vp9_alpha` and `encode_packed_alpha` too. It must divide the frame count.
- Result keys (camelCase, manifest-ready):
  - Common: `file`, `bytes`, `sha256`, `mimeType`, `codec`, `width`, `height`, `crf`, `keyint`,
    `keyframes`, `frameCount`, `fps` ("num/den"), `durationMs`, `fpsCapped`, `inputFps`,
    `inputFrameCount`, `inputIndices`.
  - VP9 adds `alpha: "native-vp9"` and `pixFmt`.
  - Packed adds `layout`, `halfWidth`, `halfHeight`, `encodedSize`, `profile`, `requiresCompositor`.
  - Loop adds `profile` and `closedGop`.
- `packed_geometry(w, h)` (new) gives the packed contract without encoding: 403x407 -> halves 404x408,
  MP4 808x408. `unpack_packed_alpha(frame, geometry)` (new) crops RGB at (0, 0, w, h) and alpha (red
  channel) at (halfWidth, 0, w, h).
- Colour: VP9 and H.264 are limited-range BT.709 and tagged through `setparams`; ffmpeg 8 otherwise
  writes primaries/transfer as unspecified (measured).
- Errors: `ForgeAVError(RuntimeError)` for missing, failed or timed-out tools, with an ASCII-escaped
  stderr tail. ValueError or TypeError for bad arguments, raised before ffmpeg starts.
  FileExistsError for no-clobber refusals.

## 2. SKILL.md routing rows

video2dsprite (paste into the capability/tooling table):

```text
| Check video tooling | scripts/forge_av.py ffmpeg_info() | functional probe: VP9 alpha must round-trip through libvpx-vp9, libx264 must encode; needs ffmpeg 5.1+ |
| Import transparent WebM | extract (forge_av.extract_frames, alpha auto) | the native VP9/VP8 decoders drop alpha; libvpx is selected automatically |
| Ship to iPhone/Safari | package packed (forge_av.encode_packed_alpha) | H.264 RGB-left/alpha-right, even halves, BT.709, faststart; needs a compositor |
```

generate2dmap (HD-2D motion step):

```text
| Encode a scene loop | scene_motion build (forge_av.encode_h264_loop) | closed GOPs whose keyint divides the loop; always QA the decoded file |
```

## 3. README tool-table rows

```text
| shared/forge_av.py (vendored) | ffmpeg helpers: functional capability probe, stream probe, WebM-alpha-safe decoding, VP9-alpha / packed-alpha / loop H.264 encoders, MP4 checks | video2dsprite, generate2dmap |
```

Requirements row: `ffmpeg 5.1+ with libvpx-vp9 and libx264 (optional: video import/export and scene loops only)`.

## 4. CHANGELOG entries

Added
- `shared/forge_av.py`, vendored into video2dsprite and generate2dmap. It provides:
  - functional ffmpeg capability probes;
  - a stream probe that reports audio tracks and cover art without mistaking them for the clip;
  - passthrough extraction and RGBA decoding with libvpx alpha, and a streaming decoder;
  - VP9-alpha, packed-alpha (even halves, BT.709, faststart) and loop-aligned closed-GOP H.264 encoders,
    with rational fps capped at 60;
  - edge-bled transparent colour, premultiplied ffmpeg scaling, and MP4 container checks.

Changed
- No existing file was edited. B05, B08 and B16 adopt the library.

BREAKING (the legacy switch, if any, belongs to the adopting module)
- Raw frame names are `frame_000000.png` (0-based, 6 digits), not `frame_0001.png`. video2dsprite `clean`
  globs `frame_*.png` and is unaffected; anything hard-coding `frame_0001.png` must change (B05).
- `extract_frames(start, duration)` keeps every frame timed in [start, start + duration). The old
  output-side `-t` dropped the last frame when start fell between frames (measured 5 frames instead of 6).
- VP9 and packed outputs are BT.709-tagged. engine_export's untagged VP9 was implicitly BT.601 (B08).

Fixed
- report v2 P1-1: subprocess text is UTF-8 with replacement; error tails are ASCII.
- engine_export.py:16-28: capability string matching is replaced by functional encode/decode probes.
- report v2 3.5: Grok's MJPEG cover is never picked as the clip, and its AAC track is reported.
- ffmpeg 8 ignored `-color_primaries/-color_trc`, so packed MP4 VUI said "unspecified". Tags now ride on
  the frames.
- WebM bytes changed on every run (random Matroska UIDs); muxing is now bitexact.
- Soft edges darkened or desaturated in VP9 and packed video because 4:2:0 chroma averaged in the black
  under alpha 0. Edge bleed cuts translucent-edge RGB MAE from 20.0 to 4.2 on a 320 px antialiased
  sprite, for about 7% more bytes.

## 5. Schema change requests

`fps` in forge_av results is a rational string ("30000/1001", "60/1"). If `animation_v3.packedAlpha.fps`
and `animation_v3.mobilePackedAlpha[].fps` are typed as numbers, accept both:

```json
"fps": {"anyOf": [{"type": "number", "exclusiveMinimum": 0},
                  {"type": "string", "pattern": "^[1-9][0-9]*/[1-9][0-9]*$"}]}
```

Optional fields producers may copy from encoder results into `packedAlpha`/`mobilePackedAlpha[]`
(allowed by `additionalProperties: true`, listed here for the docs): `keyint`, `keyframes`,
`closedGop`, `crf`, `fpsCapped`, `inputFps`, `encodedSize`, `sha256`, `bytes`.

## 6. Shared-helper promotion requests

- `forge_av._local_publish_file_no_replace(source, target)` should become `forge_core.publish_file_no_replace`.
  forge_core is vendored next to forge_av in both skills, so forge_av may import it at integration.
  A5 had no A1 dependency in Wave A, so it did not.
- `forge_av._load_frame` (Pillow `convert("RGBA")`) should use `forge_core.load_rgba` for 16-bit,
  indexed and LA inputs, with provenance.
- `forge_av._edge_bleed` could move to forge_core if other transports (WebP/AVIF exports) want the same
  edge treatment.
- A0: `shared/VENDORED.json` must map `shared/forge_av.py` to `skills/video2dsprite/scripts/forge_av.py`
  and `skills/generate2dmap/scripts/forge_av.py`. Once it does, `tools/vendor_sync.py --check` and
  `tests/test_vendored_sync.py` supersede `test_vendored_copies_match_canonical`.

## 7. Cross-module links that Z must add

- B05: route video2dsprite `extract`/`process` through `forge_av.extract_frames`. `alpha="auto"`
  replaces `--decoder libvpx-vp9`; keep the flag as an alias. `doctor` prints `forge_av.ffmpeg_info()`.
  `triage` reads `audio_streams`, `attached_pics`, `fps_rational` and `nb_frames` from `probe()`.
- B08 (engine_export 3.0):
  - Map `capabilities()`/`run()`/`encode()` to `ffmpeg_info()`/`run()`/`encode_vp9_alpha` and
    `encode_packed_alpha`; pass `keyint=frameCount` for loops.
  - For tiers, resample with `forge_core.resample_rgba`, then encode with `max_fps`.
  - `verify` uses `iter_rgba` + `unpack_packed_alpha` + `moov_before_mdat`/`duration_ms`/
    `packet_count`/`timestamps_increasing` + `probe()["vp9_alpha"]`.
- B09: packed-alpha-runtime.js currently crops alpha at x = `width` and requires `videoWidth == 2 * width`.
  Under the packedAlpha contract it must crop at x = `halfWidth` and expect `2 * halfWidth`.
  forge_av reads alpha from the red channel, like the runtime.
- B16: `scene_motion build` encodes with `encode_h264_loop` (keyint and crf from `motion_plan.encode`)
  and gates on the decoded file (see section 8 for the numbers).
- B22: forge_doctor can report `ffmpeg_info()` (functional, version, errors) as the video rung.
- Z docs:
  - Encoder checks are functional.
  - VP9 alpha needs libvpx-vp9.
  - iPhone needs packed H.264 plus a compositor.
  - Premultiply before ffmpeg scaling.
  - Loops are encoded with a loop-aligned GOP.
  - CONTRIBUTING: the `ffmpeg` marker, and tests skip without a working ffmpeg.

## 8. Known limitations and what is not proven

- **Environment.** Run only on Windows 11 with Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, scipy 1.18.1
  and ffmpeg 8.0.1 (gyan full build). The Python 3.10 floor is checked by grammar only
  (`ast.parse(feature_version=(3, 10))`). Linux and macOS were not run. ffmpeg 5.1+ is required (`-fps_mode`).
- **Determinism.** It holds per build: the same frames give the same bytes with the same
  ffmpeg/libx264/libvpx. Other builds give other bytes.
- **Playback.** Only offline decoding was checked. There is no browser, Safari, WebGL or iPhone playback
  proof. BT.709 VP9 assumes the decoder honours the VP9 colour space.
- **Loop seams.** Loop-aligned closed GOPs are necessary, not sufficient. Synthetic masked-motion loop:
  432x240, 120 frames, 16-frame smoothstep crossfade, water band over a detailed static plate. In-mask
  decoded seam/p95 (source 0.99):

  | encode | seam/p95 |
  |---|---|
  | keyint 120, crf 18 | 2.01 |
  | keyint 60, crf 18 | 2.12 |
  | keyint 40, crf 18 | 2.09 |
  | keyint 24, crf 18 | 1.96 |
  | keyint 120, crf 12 | 1.72 |
  | keyint 120, crf 18, x264 mbtree=0 | 1.59 (1.7x bytes) |
  | keyint 120, crf 14, x264 mbtree=0:ipratio=1:pbratio=1 | 1.37 (4.6x bytes) |
  | all-intra, crf 18 | 1.28 (35x bytes) |

  Every keyframe pops the same way, so shorter GOPs do not help. B08 `verify` and B16 `qa` must keep the
  decoded-seam gate. B16's "a GOP-aligned encode passes" will probably need a lower crf or another
  remedy, not only alignment. Full-frame busy motion was about 1.0 at every keyint.
- **Edge bleed.** Measured on synthetic antialiased sprites only (two fixtures).
- **fps cap.** The cap keeps the nearest frame per tick, without blending. A capped loop's period shifts
  slightly (Dusk: 0.480 s to 0.4833 s).
- **probe().**
  - Alpha means WebM AlphaMode or an alpha pix_fmt; `pal8` counts as possibly alpha.
  - Rotation is handled for multiples of 90 degrees.
  - `nb_frames` is the container count or demuxed packets, never decoded frames.
- **moov_before_mdat.** It reads top-level boxes only.
- **Packed-alpha boundary.** The runtime never samples the pad, but lossy 4:2:0 colours the pad column
  next to the content, so pad pixels are not black after decoding.
- **This worktree.** pytest prints one `PytestUnknownMarkWarning` for `ffmpeg` until A0's pytest.ini
  registers the marker. `tools/vendor_sync.py` does not exist here yet. Meanwhile
  `test_vendored_copies_match_canonical` checks both copies by sha256.
