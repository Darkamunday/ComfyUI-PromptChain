// Config — shared constants and rendering mode detection.
// All pcr-prefixed properties on nodes/elements are namespaced "PromptChain Rebuild".

// Minimum node dimensions per rendering mode.
// 1.0 and 2.0 have different padding so identical pixel values produce different visual sizes.
export const MIN_NODE_SIZE = {
  litegraph: [250, 160],
  vue: [300, 192],
};

export const CONFIG = {
  defaultWeight: 1.0,
  weightStep: 0.1,
  defaultFontSize: 13,
  minFontSize: 8,
  maxFontSize: 32,
  galleryDefaultRowHeight: 100,
  galleryMinRowHeight: 40,
  galleryMaxRowHeight: 300,
  galleryZoomStep: 20,
};

export const NODE_TYPE = "PromptChain_PromptChain";

// Returns true when ComfyUI is using Nodes 2.0 (Vue-based rendering).
export function isVueMode() {
  return window.LiteGraph?.vueNodesMode === true;
}

// Returns the minimum node size [width, height] for the active rendering mode.
export function getMinNodeSize() {
  return isVueMode() ? MIN_NODE_SIZE.vue : MIN_NODE_SIZE.litegraph;
}
