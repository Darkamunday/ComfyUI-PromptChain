// Shared lazy-load utility for Svelte bridge modules.
// Ensures the shared CSS is loaded once and caches the imported module.

import { injectStyles } from "./global-styles.js";

let cssLoaded = false;

export function ensureSvelteCSS() {
  // Global pcr-* styles (the body-portaled mode-menu dropdowns used by
  // SearchableSelect etc.) were only injected when a PromptChain NODE mounted —
  // so the viewer/gallery/sidebar, which can open with no node in the graph, got
  // unstyled dropdowns. Inject here too (idempotent) so any Svelte UI has them.
  injectStyles();
  if (cssLoaded) return;
  cssLoaded = true;
  if (!document.querySelector('link[href*="promptchain-svelte.css"]')) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = new URL("./svelte/assets/promptchain-svelte.css", import.meta.url).href;
    document.head.appendChild(link);
  }
}

export function createModuleLoader(importFn) {
  let cached = null;
  return async function loadModule() {
    if (cached) return cached;
    cached = await importFn();
    return cached;
  };
}
