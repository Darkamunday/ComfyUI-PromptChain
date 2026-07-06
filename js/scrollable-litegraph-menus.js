// PromptChain - keep long LiteGraph combo/context menus usable.

import { app } from "../../scripts/app.js";

const MENU_SELECTOR = ".litecontextmenu";
const SCROLL_CLASS = "promptchain-scrollable-litegraph-menu";
const STYLE_ID = "promptchain-scrollable-litegraph-menu-style";

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;

  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    .${SCROLL_CLASS} {
      max-height: 65vh;
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior: contain;
    }
  `;
  document.head.appendChild(style);
}

function makeScrollable(menu) {
  if (!(menu instanceof HTMLElement)) return;
  menu.classList.add(SCROLL_CLASS);
}

function applyExistingMenus(root = document) {
  if (root instanceof HTMLElement && root.matches(MENU_SELECTOR)) {
    makeScrollable(root);
  }
  root.querySelectorAll?.(MENU_SELECTOR).forEach(makeScrollable);
}

app.registerExtension({
  name: "PromptChain.ScrollableLiteGraphMenus",
  setup() {
    ensureStyle();
    applyExistingMenus();

    // LiteGraph renders combo dropdowns and context menus as global
    // .litecontextmenu elements under document.body, outside the PromptChain
    // node DOM. That means there is no reliable PromptChain-only selector at
    // menu creation time, so this patch is global but limited to LiteGraph menu
    // elements and only adds scroll containment for long menus.
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node instanceof HTMLElement) {
            applyExistingMenus(node);
          }
        }
      }
    });

    observer.observe(document.body || document.documentElement, {
      childList: true,
      subtree: true,
    });
  },
});
