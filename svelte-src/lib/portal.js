// Svelte action: portals a DOM node to document.body (for modals, overlays, etc.)
export function portal(node) {
  document.body.appendChild(node);
  return { destroy() { node.parentNode?.removeChild(node); } };
}
