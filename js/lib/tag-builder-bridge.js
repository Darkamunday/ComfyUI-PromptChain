// Tag Builder bridge — lazy-loads the Svelte tag builder and manages its lifecycle.
// Public API: showTagBuilder(view, from, to, options), hideTagBuilder()

import { getTagSourceConfig, setTagSourceConfig } from "./tags-dropdown.js";
import { ensureSvelteCSS, createModuleLoader } from "./lazy-load.js";

let currentInstance = null;
let currentContainer = null;
let tbCssLoaded = false;

function ensureCSS() {
  ensureSvelteCSS();
  if (tbCssLoaded) return;
  tbCssLoaded = true;
  const tbLink = document.createElement("link");
  tbLink.rel = "stylesheet";
  tbLink.href = new URL("./tag-builder.css", import.meta.url).href;
  document.head.appendChild(tbLink);
}

let svelteModule = null;
const loadModule = createModuleLoader(async () => {
  svelteModule = await import("./svelte/promptchain-tag-builder.js");
  return svelteModule;
});

export async function showTagBuilder(view, from = 0, to = 0, options = {}) {
  ensureCSS();
  hideTagBuilder();

  const mod = await loadModule();

  currentContainer = document.createElement("div");
  document.body.appendChild(currentContainer);

  const tagSourceConfig = getTagSourceConfig();

  currentInstance = mod.mountTagBuilder(currentContainer, {
    from,
    to,
    initialTab: options.initialTab || "all",
    initialQuery: options.initialQuery || "",
    tagSourceConfig,
    onPromptStyleChange: (style) => setTagSourceConfig({ prompt_style: style }),
    onInsert: (text) => {
      if (text) {
        const doc = view.state.doc;
        const lineStart = doc.lineAt(from).from;
        const isAtLineStart = from === lineStart || from === 0;

        let result = text;
        // Add separator if appending mid-line
        if (from > 0 && !isAtLineStart) {
          const textBefore = doc.sliceString(Math.max(0, from - 1), from);
          if (textBefore && !/\s/.test(textBefore)) {
            result = (tagSourceConfig.prompt_style === "natural" ? " " : ", ") + result;
          }
        }

        view.dispatch({
          changes: { from, to, insert: result },
          selection: { anchor: from + result.length },
        });
      }
      hideTagBuilder();
      view.focus();
    },
    onClose: () => {
      hideTagBuilder();
      view.focus();
    },
  });
}

export function hideTagBuilder() {
  if (currentInstance) {
    svelteModule?.destroyTagBuilder(currentInstance);
    currentInstance = null;
  }
  if (currentContainer) {
    currentContainer.remove();
    currentContainer = null;
  }
}
