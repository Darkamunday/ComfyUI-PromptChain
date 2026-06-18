// Layer engine for the viewer Edit modal — pure, DOM-free, browser+node ESM.
//
// The brush engine's no-rings guarantee comes from writing OPAQUE final colors
// with a SINGLE float blend + ONE quantization per pixel (see EditModal's brush
// notes). A single paint canvas could bake that at write time; layers can't —
// each layer must keep its own un-flattened contribution and the flatten has to
// happen at DISPLAY time, still with exactly one quantization, or the rings
// (8-bit per-step terraces / premultiplied staircase) come back.
//
// Layer storage = STRAIGHT (non-premultiplied) RGB 0..255 + coverage 0..1 in a
// Float32 RGBA buffer (A slot = coverage). The base image is just a layer with
// coverage 1 everywhere. `composite()` blends bottom→top in float and quantizes
// once with the same integer-hash dither the brush uses, so a smooth coverage
// ramp resolves to a smooth gradient instead of banded terraces.

// Per-pixel integer-hash white noise in [-0.5, 0.5] — deterministic per
// position so re-compositing identical layers never shimmers. VERBATIM from
// EditModal's `dither` (keep in sync; the equivalence harness asserts it).
export function ditherAt(x, y) {
  let h = (x * 374761393 + y * 668265263) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296 - 0.5;
}

let _nextLayerId = 1;

// A blank transparent layer. `data` is Float32 RGBA (RGB 0..255, A = coverage
// 0..1). `mask`, when present, is a Float32 coverage field multiplied into the
// layer's alpha at composite time (a real per-layer mask).
export function createLayer(width, height, opts = {}) {
  return {
    id: opts.id ?? _nextLayerId++,
    name: opts.name ?? "Layer",
    visible: opts.visible ?? true,
    opacity: opts.opacity ?? 1,
    blendMode: opts.blendMode ?? "normal",
    locked: opts.locked ?? false,
    data: new Float32Array(width * height * 4),
    mask: opts.mask ?? null,
  };
}

// Seed a layer from RGBA pixel bytes (e.g. the source image's ImageData.data) —
// coverage 1 everywhere it's opaque, scaled by the byte alpha otherwise.
export function layerFromRGBA(bytes, width, height, opts = {}) {
  const layer = createLayer(width, height, opts);
  const d = layer.data;
  for (let i = 0, n = width * height; i < n; i++) {
    d[i * 4] = bytes[i * 4];
    d[i * 4 + 1] = bytes[i * 4 + 1];
    d[i * 4 + 2] = bytes[i * 4 + 2];
    d[i * 4 + 3] = bytes[i * 4 + 3] / 255;
  }
  return layer;
}

// Straight-alpha "over" of one layer onto a resolved float RGB accumulator.
// Only "normal" is implemented; unknown modes fall back to normal for now.
function blendInto(accRGB, layer, width, height) {
  const d = layer.data;
  const op = layer.opacity;
  const mask = layer.mask;
  for (let i = 0, n = width * height; i < n; i++) {
    let a = d[i * 4 + 3] * op;
    if (mask) a *= mask[i];
    if (a <= 0) continue;
    const inv = 1 - a;
    const k = i * 3;
    accRGB[k] = d[i * 4] * a + accRGB[k] * inv;
    accRGB[k + 1] = d[i * 4 + 1] * a + accRGB[k + 1] * inv;
    accRGB[k + 2] = d[i * 4 + 2] * a + accRGB[k + 2] * inv;
  }
}

// Flatten visible layers (array order = bottom→top) into opaque RGBA bytes,
// quantizing ONCE with dither. Returns Uint8ClampedArray(width*height*4); the
// browser wraps it in `new ImageData(bytes, width, height)`. The bottom layer
// is expected to be opaque (the base image); uncovered pixels resolve to black.
export function composite(layers, width, height, dither = ditherAt) {
  const n = width * height;
  const accRGB = new Float32Array(n * 3);
  for (const layer of layers) {
    if (layer.visible) blendInto(accRGB, layer, width, height);
  }
  const out = new Uint8ClampedArray(n * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = y * width + x;
      const dq = dither(x, y);
      out[i * 4] = Math.round(accRGB[i * 3] + dq);
      out[i * 4 + 1] = Math.round(accRGB[i * 3 + 1] + dq);
      out[i * 4 + 2] = Math.round(accRGB[i * 3 + 2] + dq);
      out[i * 4 + 3] = 255;
    }
  }
  return out;
}
