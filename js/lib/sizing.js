// Sizing — enforces minimum node width in Vue 2.0 mode.
// The 2.0 resize handler reads inline min-width from the node element.
// This must be re-applied after graph load and mode switches because
// Vue destroys/recreates DOM elements, losing inline styles.

import { MIN_NODE_SIZE, isVueMode } from "./config.js";

export function applyVueMinWidthAll(graph) {
  if (!isVueMode() || !graph?._nodes) return;
  for (const node of graph._nodes) {
    if (!node._pcrHasMinSize) continue;
    const element = document.querySelector(`[data-node-id="${node.id}"]`);
    if (!element) continue;
    const current = element.style.getPropertyValue("min-width");
    if (!current || parseFloat(current) < MIN_NODE_SIZE.vue[0]) {
      element.style.setProperty("min-width", `${MIN_NODE_SIZE.vue[0]}px`);
    }
  }
}
