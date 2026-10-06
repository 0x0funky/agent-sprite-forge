/* Packed-alpha playback, thin wrapper. The application owns scheduling, visibility
   and disposal. Serve media over HTTP(S); cross-origin media needs CORS (set
   crossOrigin before src). The shared WebGL compositor and its Canvas2D CPU
   fallback live in runtime/packed-alpha-webgl.mjs; timing, distance-driven walks,
   hit-stop and transitions live in runtime/forge-runtime.mjs. Copy the runtime
   folder together with this file. */
import {sharedPackedAlphaCompositor} from './runtime/packed-alpha-webgl.mjs';

export {
  createPackedAlphaCompositor, packedGeometry, sharedPackedAlphaCompositor,
} from './runtime/packed-alpha-webgl.mjs';

/* Rebuild a straight-alpha drawable from a packed video (RGB left, alpha right).
   metadata is animation.json packedAlpha (2.0 or 3.0) or a mobilePackedAlpha
   tier. Returns {drawable, update(force), lease, release()}: update() composes
   the current decoded frame when it changed and returns true, and throws when
   the video does not match the metadata. Every drawable shares one WebGL
   context (options apply to the first call); without WebGL the CPU path is used.
   Pass options.compositor to use your own compositor. */
export function createPackedAlphaDrawable(video, metadata, options = {}) {
  const compositor = options.compositor ?? sharedPackedAlphaCompositor(options);
  const lease = compositor.acquire(video, metadata, options);
  return {
    drawable: lease.drawable,
    lease,
    update(force = false) {
      const composed = lease.update(force);
      if (lease.failed()) throw new Error(lease.error);
      return composed;
    },
    release: () => lease.release(),
  };
}

/* Draw a frame at its original source anchor, excluding even-padding pixels.
   sourceRect allows a single union crop without changing world placement.
   drawable is a canvas/image, or the object createPackedAlphaDrawable returns
   (its lease region is then used, which also covers scaled mobile tiers).
   options: {region: [x, y, w, h] of the drawable that maps onto sourceRect,
   snap: true to place the frame on whole pixels (pixel art at integer scale)}.
   For Three.js use the drawable as a CanvasTexture with the same geometry. */
export function drawAnchoredFrame(ctx, drawable, clip, x, y, scale = 1, options = {}) {
  const source = drawable && drawable.drawable ? drawable.drawable : drawable;
  const leaseRegion = drawable && (drawable.region ?? drawable.lease?.region);
  const [rx, ry, rw, rh] = clip.sourceRect;
  const [ax, ay] = clip.sourceAnchor;
  const content = clip.contentSize ?? [source.width, source.height];
  const [sx, sy, sw, sh] = options.region ?? leaseRegion ?? [0, 0, content[0], content[1]];
  let left = x + (rx - ax) * scale, top = y + (ry - ay) * scale;
  if (options.snap) {
    left = Math.round(left);
    top = Math.round(top);
  }
  ctx.drawImage(source, sx, sy, sw, sh, left, top, rw * scale, rh * scale);
}
