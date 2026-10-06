/**
 * packed-alpha-webgl.mjs: rebuild straight-alpha sprites from packed-alpha H.264
 * (RGB in the left half, alpha in the red channel of the right half) with one
 * shared WebGL context, and a Canvas2D CPU fallback.
 *
 * Geometry follows forge_av.packed_geometry and animation.json: width/height are
 * the logical frame, halfWidth/halfHeight the even physical halves, so the video
 * is (2 * halfWidth) x halfHeight; animation.json 2.0 has no halves (its
 * width/height already are the half). RGB is cropped at (0, 0, width, height)
 * and alpha at (halfWidth, 0, width, height).
 *
 * The compositor:
 *   - uses ONE WebGL context and one scratch canvas for every clip; each lease
 *     copies its frame into its own 2D canvas (lease.drawable), so many actors
 *     never create many contexts or read pixels back on the CPU;
 *   - clamps samples half a texel inside the content (the seam clamp), so linear
 *     filtering never mixes RGB with the alpha half or the padding column;
 *   - snaps alpha <= 2/255 to 0 and >= 253/255 to 1 (codec noise) and writes
 *     premultiplied colour into a premultipliedAlpha canvas;
 *   - reuses its texture (texSubImage2D unless the video size changes), vertex
 *     buffer and scratch canvas (grown, never shrunk);
 *   - survives WebGL context loss: it composes on the CPU until the context is
 *     restored, then rebuilds its GPU resources;
 *   - can draw an optional rim/ink outline around the silhouette (WebGL only).
 *
 * Control playback with lease.video (play, pause, currentTime, playbackRate);
 * draw lease.drawable. Never draw the packed video itself. Cross-origin media
 * needs CORS (crossOrigin set before src) for both the WebGL and CPU paths.
 * Serve .mjs files with a JavaScript MIME type.
 */
import {PACKED_LAYOUT, clamp, packedGeometry} from './forge-runtime.mjs';

export {PACKED_LAYOUT, packedGeometry};

// Thresholds sit half a code value away from the 8-bit levels they separate: a GPU
// samples 2/255 or 253/255 with float rounding, so comparing at the exact level
// would snap some texels and not others (measured on SwiftShader).
export const ALPHA_SNAP_LOW = 2.5 / 255;
export const ALPHA_SNAP_HIGH = 252.5 / 255;
export const DEFAULT_OUTLINE = Object.freeze({
  rim: Object.freeze([0.98, 0.94, 0.80]),
  ink: Object.freeze([0.14, 0.22, 0.20]),
  rimPx: 1.35,
  inkScale: 1.65,
  rimStrength: 0.88,
  inkStrength: 0.40,
});
const ERROR_CHECK_INTERVAL = 30;

/** Codec-noise snap for alpha in [0, 1]: below `low` -> 0, above `high` -> 1 (8-bit 0..2 -> 0, 253..255 -> 1). */
export function alphaSnap(alpha, low = ALPHA_SNAP_LOW, high = ALPHA_SNAP_HIGH) {
  if (alpha < low) return 0;
  if (alpha > high) return 1;
  return alpha;
}

/** The same snap for 8-bit alpha (2 -> 0, 253 -> 255). */
export function alphaSnapByte(alpha, low = 2, high = 253) {
  if (alpha <= low) return 0;
  if (alpha >= high) return 255;
  return alpha;
}

/** Clamp a texel coordinate half a texel inside [0, extent): bilinear taps then stay inside the content. */
export function seamClamp(texel, extent) {
  return clamp(texel, 0.5, extent - 0.5);
}

/**
 * Normalized texture coordinates sampled for output point (u, v) in [0, 1]^2
 * (v down): {rgb: [s, t], alpha: [s, t]}, after the seam clamp. The fragment
 * shader computes exactly this.
 */
export function packedSampleCoords(u, v, geometry) {
  const x = seamClamp(u * geometry.width, geometry.width);
  const y = seamClamp(v * geometry.height, geometry.height);
  const textureWidth = 2 * geometry.halfWidth, textureHeight = geometry.halfHeight;
  return {
    rgb: [x / textureWidth, y / textureHeight],
    alpha: [(geometry.halfWidth + x) / textureWidth, y / textureHeight],
  };
}

/**
 * CPU compositor kernel. src: RGBA bytes of one decoded packed frame, srcWidth
 * pixels per row (at least 2 * halfWidth). out: RGBA bytes of width x height.
 * Writes straight alpha (ready for putImageData) with RGB zeroed where alpha is
 * 0; alpha comes from the red channel of the right half, like
 * forge_av.unpack_packed_alpha. options.snap (default true) applies alphaSnapByte.
 */
export function composePackedPixels(src, srcWidth, geometry, out, {snap = true} = {}) {
  const {width, height, halfWidth} = geometry;
  if (srcWidth < 2 * halfWidth || src.length < srcWidth * height * 4 || out.length < width * height * 4) {
    throw new RangeError('Packed frame or output buffer is smaller than the packed geometry');
  }
  for (let y = 0; y < height; y++) {
    const row = y * srcWidth * 4, outRow = y * width * 4;
    for (let x = 0; x < width; x++) {
      const colour = row + x * 4, o = outRow + x * 4;
      const raw = src[row + (halfWidth + x) * 4];
      const alpha = snap ? alphaSnapByte(raw) : raw;
      out[o] = alpha ? src[colour] : 0;
      out[o + 1] = alpha ? src[colour + 1] : 0;
      out[o + 2] = alpha ? src[colour + 2] : 0;
      out[o + 3] = alpha;
    }
  }
  return out;
}

export const VERTEX_SHADER = `attribute vec2 aPosition;
varying vec2 vUv;
void main() {
  vUv = vec2((aPosition.x + 1.0) * 0.5, (1.0 - aPosition.y) * 0.5);
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

export const FRAGMENT_SHADER = `#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform sampler2D uMovie;
uniform vec2 uContent;
uniform vec2 uHalf;
uniform vec2 uSnap;
uniform float uOutline;
uniform vec3 uRimColor;
uniform vec3 uInkColor;
uniform vec4 uOutlineShape;
varying vec2 vUv;

vec2 movieUv(vec2 texel, float offset) {
  return vec2((offset + texel.x) / (2.0 * uHalf.x), texel.y / uHalf.y);
}

vec2 seamClamp(vec2 texel) {
  return clamp(texel, vec2(0.5), uContent - vec2(0.5));
}

float alphaAt(vec2 texel) {
  if (texel.x < 0.0 || texel.y < 0.0 || texel.x >= uContent.x || texel.y >= uContent.y) return 0.0;
  return texture2D(uMovie, movieUv(seamClamp(texel), uHalf.x)).r;
}

float snapAlpha(float alpha) {
  if (alpha < uSnap.x) return 0.0;
  if (alpha > uSnap.y) return 1.0;
  return alpha;
}

float ring(vec2 texel, float radius) {
  float d = radius * 0.70710678;
  float a = max(max(alphaAt(texel + vec2(radius, 0.0)), alphaAt(texel - vec2(radius, 0.0))),
                max(alphaAt(texel + vec2(0.0, radius)), alphaAt(texel - vec2(0.0, radius))));
  a = max(a, max(alphaAt(texel + vec2(d, d)), alphaAt(texel - vec2(d, d))));
  return max(a, max(alphaAt(texel + vec2(d, -d)), alphaAt(texel - vec2(d, -d))));
}

void main() {
  vec2 texel = vUv * uContent;
  vec3 rgb = texture2D(uMovie, movieUv(seamClamp(texel), 0.0)).rgb;
  float alpha = snapAlpha(alphaAt(texel));
  vec4 colour = vec4(rgb * alpha, alpha);
  if (uOutline > 0.5) {
    float rim = smoothstep(0.08, 0.65, ring(texel, uOutlineShape.x)) * uOutlineShape.z;
    float ink = smoothstep(0.08, 0.65, ring(texel, uOutlineShape.x * uOutlineShape.y)) * uOutlineShape.w;
    float cover = 1.0 - colour.a;
    colour = vec4(colour.rgb + (uRimColor * rim + uInkColor * ink * (1.0 - rim)) * cover,
                  colour.a + (rim + ink * (1.0 - rim)) * cover);
  }
  gl_FragColor = colour;
}
`;

const CONTEXT_ATTRIBUTES = Object.freeze({
  alpha: true, premultipliedAlpha: true, preserveDrawingBuffer: true,
  antialias: false, depth: false, stencil: false,
});

function message(error) {
  return String(error?.message ?? error);
}

function outlineSettings(value) {
  if (!value) return null;
  const settings = {...DEFAULT_OUTLINE, ...(value === true ? {} : value)};
  return Object.freeze(settings);
}

/**
 * Create a compositor. options:
 *   document       the DOM document (default globalThis.document)
 *   createCanvas   (width, height) -> canvas; default document.createElement('canvas')
 *   webgl          false forces the CPU path (default: WebGL when available)
 *   alphaSnap      false keeps raw codec alpha (default true)
 *   outline        default outline for every lease: true or {rim, ink, rimPx, inkScale, rimStrength, inkStrength}
 *   onError        (message, lease) -> void, called once per failed lease
 *
 * acquire(video, packedMeta, {outline, region, onError}) returns a lease:
 *   {video, drawable, geometry, region, mode, frameVersion, error,
 *    update(force) -> true when a new frame was composed, ready(), failed(), release()}.
 * Call lease.update() from the game's render loop; it composes only when the
 * decoded frame changed (requestVideoFrameCallback where available).
 */
export function createPackedAlphaCompositor(options = {}) {
  const doc = options.document ?? globalThis.document;
  const createCanvas = options.createCanvas ?? ((width, height) => {
    if (!doc?.createElement) throw new Error('No document: pass options.createCanvas');
    const canvas = doc.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    return canvas;
  });
  const snap = options.alphaSnap === false ? [-1, 2] : [ALPHA_SNAP_LOW, ALPHA_SNAP_HIGH];
  const defaultOutline = outlineSettings(options.outline);
  const leases = new Set(), errors = [];
  const stats = {
    leases: 0, composedFrames: 0, webglFrames: 0, cpuFrames: 0, textureAllocations: 0, textureUpdates: 0,
    bufferResizes: 0, contextLosses: 0, contextRestores: 0, gpuErrorChecks: 0, failed: 0, outlineSkipped: 0,
  };
  // WebGL state: idle (not created), ready, lost, restored, unavailable, failed, disabled.
  let state = options.webgl === false ? 'disabled' : 'idle';
  let gl = null, glCanvas = null, resources = null, destroyed = false;
  let cpuCanvas = null, cpuContext = null;

  function remember(error) {
    const text = message(error);
    if (errors.length < 32 && !errors.includes(text)) errors.push(text);
    return text;
  }

  function onContextLost(event) {
    event?.preventDefault?.();
    if (state !== 'ready' && state !== 'restored') return;
    state = 'lost';
    resources = null;
    stats.contextLosses++;
  }

  function onContextRestored() {
    if (state !== 'lost') return;
    state = 'restored';
    stats.contextRestores++;
  }

  function compile(type, source, shaders) {
    const shader = gl.createShader(type);
    if (!shader) throw new Error('Cannot allocate a packed-alpha shader');
    shaders.push(shader);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(`Packed-alpha shader failed to compile: ${gl.getShaderInfoLog(shader)}`);
    }
    return shader;
  }

  function buildResources() {
    const shaders = [];
    const program = gl.createProgram();
    if (!program) throw new Error('Cannot allocate a packed-alpha program');
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX_SHADER, shaders));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER, shaders));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`Packed-alpha program failed to link: ${gl.getProgramInfoLog(program)}`);
    }
    const buffer = gl.createBuffer(), texture = gl.createTexture();
    if (!buffer || !texture) throw new Error('Cannot allocate packed-alpha GPU buffers');
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'aPosition');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    // Video rows arrive top first (texture v = 0); the vertex shader maps the
    // viewport top to v = 0, so nothing is flipped on upload.
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.disable(gl.BLEND);
    gl.clearColor(0, 0, 0, 0);
    const uniform = name => gl.getUniformLocation(program, name);
    gl.uniform1i(uniform('uMovie'), 0);
    resources = {
      program, buffer, texture, shaders, textureWidth: 0, textureHeight: 0,
      content: uniform('uContent'), half: uniform('uHalf'), snap: uniform('uSnap'), outline: uniform('uOutline'),
      rimColor: uniform('uRimColor'), inkColor: uniform('uInkColor'), shape: uniform('uOutlineShape'),
    };
  }

  /** True when the WebGL path can compose now; creates or rebuilds resources lazily. */
  function webglReady() {
    if (state === 'ready') return !gl.isContextLost();
    if (state === 'idle') {
      try {
        glCanvas = createCanvas(1, 1);
        gl = glCanvas.getContext('webgl', CONTEXT_ATTRIBUTES)
          ?? glCanvas.getContext('experimental-webgl', CONTEXT_ATTRIBUTES);
      } catch (error) {
        remember(error);
        gl = null;
      }
      if (!gl) {
        state = 'unavailable';
        return false;
      }
      glCanvas.addEventListener?.('webglcontextlost', onContextLost);
      glCanvas.addEventListener?.('webglcontextrestored', onContextRestored);
      state = 'restored';
    }
    if (state === 'restored') {
      if (gl.isContextLost()) return false;
      try {
        buildResources();
        state = 'ready';
        return true;
      } catch (error) {
        remember(error);
        // A context lost in the middle of a rebuild waits for its restore event; anything else is final.
        state = gl.isContextLost() ? 'lost' : 'failed';
        return false;
      }
    }
    return false;
  }

  function composeWebGL(lease) {
    const {width, height, halfWidth, halfHeight} = lease.geometry, video = lease.video;
    const r = resources;
    if (glCanvas.width < width) { glCanvas.width = width; stats.bufferResizes++; }
    if (glCanvas.height < height) { glCanvas.height = height; stats.bufferResizes++; }
    gl.viewport(0, 0, width, height);
    gl.useProgram(r.program);
    gl.bindTexture(gl.TEXTURE_2D, r.texture);
    gl.uniform2f(r.content, width, height);
    gl.uniform2f(r.half, halfWidth, halfHeight);
    gl.uniform2f(r.snap, snap[0], snap[1]);
    const outline = lease.outline;
    gl.uniform1f(r.outline, outline ? 1 : 0);
    if (outline) {
      gl.uniform3f(r.rimColor, ...outline.rim);
      gl.uniform3f(r.inkColor, ...outline.ink);
      gl.uniform4f(r.shape, outline.rimPx, outline.inkScale, outline.rimStrength, outline.inkStrength);
    }
    if (r.textureWidth !== video.videoWidth || r.textureHeight !== video.videoHeight) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
      r.textureWidth = video.videoWidth;
      r.textureHeight = video.videoHeight;
      stats.textureAllocations++;
    } else {
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, video);
      stats.textureUpdates++;
    }
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    // getError is a synchronous driver query: check each lease's first frame, then periodically.
    if (lease.frameVersion === 0 || stats.webglFrames % ERROR_CHECK_INTERVAL === 0) {
      stats.gpuErrorChecks++;
      const code = gl.getError();
      if (code !== gl.NO_ERROR) throw new Error(`Packed-alpha GPU composition failed (GL error ${code})`);
    }
    if (gl.isContextLost()) throw new Error('WebGL context lost during composition');
    // The viewport sits in the lower-left corner of the scratch drawing buffer.
    lease.context.clearRect(0, 0, width, height);
    lease.context.drawImage(glCanvas, 0, glCanvas.height - height, width, height, 0, 0, width, height);
    stats.webglFrames++;
  }

  function composeCPU(lease) {
    const {width, height, videoWidth, videoHeight} = lease.geometry;
    if (!cpuCanvas) {
      cpuCanvas = createCanvas(videoWidth, videoHeight);
      cpuContext = cpuCanvas.getContext('2d', {willReadFrequently: true});
      if (!cpuContext) throw new Error('Canvas 2D is unavailable for the CPU fallback');
    }
    if (cpuCanvas.width < videoWidth) cpuCanvas.width = videoWidth;
    if (cpuCanvas.height < videoHeight) cpuCanvas.height = videoHeight;
    cpuContext.clearRect(0, 0, videoWidth, videoHeight);
    cpuContext.drawImage(lease.video, 0, 0);
    const source = cpuContext.getImageData(0, 0, videoWidth, videoHeight);
    if (!lease.imageData) lease.imageData = lease.context.createImageData(width, height);
    composePackedPixels(source.data, videoWidth, lease.geometry, lease.imageData.data, {snap: snap[0] >= 0});
    lease.context.putImageData(lease.imageData, 0, 0);
    if (lease.outline) stats.outlineSkipped++;
    stats.cpuFrames++;
  }

  function compose(lease) {
    if (webglReady()) {
      try {
        composeWebGL(lease);
        return 'webgl';
      } catch (error) {
        if (error?.name === 'SecurityError') throw error;
        remember(error);
        if (gl?.isContextLost()) {
          if (state === 'ready') onContextLost(null);
        } else {
          state = 'failed';
        }
      }
    }
    composeCPU(lease);
    return 'cpu';
  }

  function acquire(video, meta, leaseOptions = {}) {
    if (destroyed) throw new Error('The packed-alpha compositor has been destroyed');
    if (!video) throw new TypeError('acquire needs a video element');
    const geometry = packedGeometry(meta);
    const drawable = createCanvas(geometry.width, geometry.height);
    const context = drawable.getContext('2d');
    if (!context) throw new Error('Canvas 2D is unavailable for the packed-alpha output');
    let released = false, failed = false, error = null, ready = false;
    let presented = 0, frameCallback = null, lastToken = null;
    const onError = leaseOptions.onError ?? options.onError;
    const lease = {
      video, drawable, context, geometry,
      region: leaseOptions.region ?? null,
      outline: leaseOptions.outline === undefined ? defaultOutline : outlineSettings(leaseOptions.outline),
      mode: null,
      frameVersion: 0,
      imageData: null,
      get error() { return error; },
      ready: () => !released && ready && !failed,
      failed: () => failed,
      update(force = false) {
        if (released || failed || destroyed) return false;
        if (!(video.readyState >= 2) || video.seeking) return false;
        const token = `${presented}:${video.currentTime}`;
        if (!force && token === lastToken) return false;
        if (video.videoWidth !== geometry.videoWidth || video.videoHeight !== geometry.videoHeight) {
          fail(`Packed alpha video is ${video.videoWidth}x${video.videoHeight}; the manifest needs `
            + `${geometry.videoWidth}x${geometry.videoHeight}`);
          return false;
        }
        try {
          lease.mode = compose(lease);
        } catch (cause) {
          fail(cause?.name === 'SecurityError'
            ? `Packed alpha video is cross-origin without CORS: ${message(cause)}` : cause);
          return false;
        }
        lastToken = token;
        lease.frameVersion++;
        stats.composedFrames++;
        ready = true;
        return true;
      },
      release() {
        if (released) return;
        released = true;
        if (frameCallback !== null) video.cancelVideoFrameCallback?.(frameCallback);
        frameCallback = null;
        leases.delete(lease);
        drawable.width = 1;
        drawable.height = 1;
        lease.imageData = null;
      },
    };
    function fail(cause) {
      if (failed) return;
      failed = true;
      error = remember(cause);
      stats.failed++;
      try { onError?.(error, lease); } catch (callbackError) { remember(callbackError); }
    }
    if (typeof video.requestVideoFrameCallback === 'function') {
      const onFrame = (_now, metadata) => {
        presented = metadata?.presentedFrames ?? presented + 1;
        frameCallback = released ? null : video.requestVideoFrameCallback(onFrame);
      };
      frameCallback = video.requestVideoFrameCallback(onFrame);
    }
    leases.add(lease);
    stats.leases++;
    return lease;
  }

  function destroy() {
    if (destroyed) return;
    for (const lease of [...leases]) lease.release();
    destroyed = true;
    if (gl && resources && !gl.isContextLost()) {
      try {
        gl.deleteTexture(resources.texture);
        gl.deleteBuffer(resources.buffer);
        gl.deleteProgram(resources.program);
        for (const shader of resources.shaders) gl.deleteShader(shader);
      } catch (error) {
        remember(error);
      }
    }
    if (glCanvas) {
      glCanvas.removeEventListener?.('webglcontextlost', onContextLost);
      glCanvas.removeEventListener?.('webglcontextrestored', onContextRestored);
      try { gl?.getExtension?.('WEBGL_lose_context')?.loseContext(); } catch (error) { remember(error); }
      glCanvas.width = 1;
      glCanvas.height = 1;
    }
    if (cpuCanvas) {
      cpuCanvas.width = 1;
      cpuCanvas.height = 1;
    }
    gl = null;
    resources = null;
    state = 'disabled';
  }

  return {
    acquire,
    destroy,
    get destroyed() { return destroyed; },
    /** 'webgl' while the GPU path is in use, 'pending' before it is first tried or rebuilt, else 'cpu'. */
    get mode() {
      if (state === 'ready') return 'webgl';
      return state === 'idle' || state === 'restored' ? 'pending' : 'cpu';
    },
    get state() { return state; },
    getStats: () => ({
      ...stats, state, activeLeases: leases.size, errors: errors.slice(),
      scratchSize: glCanvas ? [glCanvas.width, glCanvas.height] : null,
    }),
  };
}

let shared = null;

/** The process-wide compositor (one WebGL context for the whole page); options apply on first use. */
export function sharedPackedAlphaCompositor(options = {}) {
  if (!shared || shared.destroyed) shared = createPackedAlphaCompositor(options);
  return shared;
}
