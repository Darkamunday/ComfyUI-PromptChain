// Single source of truth for gallery thumbnail zoom. Two paths reach the
// same persisted pcrGalleryRowHeight: isolation.js's window-capture wheel
// handler → OutputPanel.updateGalleryZoom (node mode, where capture-phase
// stopImmediatePropagation suppresses the gallery's own listener), and
// GeneratedGallery's own wheel handler (fullscreen overlay, where it runs
// first and stops propagation). They previously used different steps and
// clamp ranges (±15 @ 40–300 vs ±25 @ 60–400), so a value pushed past one
// path's max left the other path's scroll clicks dead.
export const GALLERY_ROW_HEIGHT_MIN = 40;
export const GALLERY_ROW_HEIGHT_MAX = 400;

// ~15% per wheel notch: a fixed pixel step is a 40% jump at small sizes but
// vanishes inside the justified layout's repartition tolerance at large
// sizes; a proportional step stays visible across the whole range.
const ZOOM_FACTOR = 1.15;

export function clampGalleryRowHeight(value) {
  return Math.max(GALLERY_ROW_HEIGHT_MIN, Math.min(GALLERY_ROW_HEIGHT_MAX, value));
}

export function zoomGalleryRowHeight(current, direction) {
  if (!direction) return clampGalleryRowHeight(current);
  const next = direction > 0 ? current * ZOOM_FACTOR : current / ZOOM_FACTOR;
  return clampGalleryRowHeight(Math.round(next));
}
