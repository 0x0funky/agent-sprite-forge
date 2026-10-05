/* Integration example: application owns scheduling, visibility and disposal.
   Serve media over HTTP(s); cross-origin media needs CORS for pixel readback. */
export function createPackedAlphaDrawable(video, metadata) {
  if (metadata.layout !== 'rgb-left-alpha-right') throw new Error('Unsupported packed alpha layout');
  const w = metadata.width, h = metadata.height;
  if (!Number.isInteger(w) || !Number.isInteger(h) || w < 1 || h < 1) throw new Error('Invalid geometry');
  const makeCanvas = () => Object.assign(document.createElement('canvas'), { width: w, height: h });
  const drawable = makeCanvas(), alphaCanvas = makeCanvas();
  const color = drawable.getContext('2d', { willReadFrequently: true });
  const alpha = alphaCanvas.getContext('2d', { willReadFrequently: true });
  if (!color || !alpha) throw new Error('Canvas 2D unavailable');
  let lastTime = -1;
  return {
    drawable,
    update() {
      if (video.readyState < 2 || video.currentTime === lastTime) return false;
      if (video.videoWidth !== w * 2 || video.videoHeight !== h) throw new Error('Packed video size mismatch');
      color.clearRect(0, 0, w, h);
      color.drawImage(video, 0, 0, w, h, 0, 0, w, h);
      alpha.drawImage(video, w, 0, w, h, 0, 0, w, h);
      const pixels = color.getImageData(0, 0, w, h);
      const mask = alpha.getImageData(0, 0, w, h).data;
      for (let i = 0; i < pixels.data.length; i += 4) pixels.data[i + 3] = mask[i];
      color.putImageData(pixels, 0, 0);
      lastTime = video.currentTime;
      return true;
    }
  };
}

/* Draw a frame at its original source anchor. Excludes even-padding pixels.
   sourceRect allows a single union crop without changing world placement.
   For Three.js use drawable as CanvasTexture and the same source geometry. */
export function drawAnchoredFrame(ctx, drawable, clip, x, y, scale = 1) {
  const [rx, ry, rw, rh] = clip.sourceRect;
  const [ax, ay] = clip.sourceAnchor;
  const [cw, ch] = clip.contentSize;
  ctx.drawImage(drawable, 0, 0, cw, ch,
    x + (rx - ax) * scale, y + (ry - ay) * scale, rw * scale, rh * scale);
}
