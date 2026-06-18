// Wildcard mode dropdown — shared popup for changing per-wildcard mode.
// Used by Network panel wildcard rows and (future) editor inline badges.

import { app } from "../../../scripts/app.js";
import { closeActivePopup, isPopupOpen, createSearchableList, openPopup, positionPopup } from "./popup-menu.js";

const MODE_EMOJI = {
  randomize: "\uD83C\uDFB2",  // dice
  combine: "\uD83D\uDCDA",    // books
  iterate: "\u267B\uFE0F",    // recycle
  none: "\u274C",              // cross
};

// Fetch wildcard options from the API (cached per session).
const _optionsCache = new Map();

async function fetchWildcardOptions(name) {
  if (_optionsCache.has(name)) return _optionsCache.get(name);
  try {
    const res = await fetch(`/promptchain/wildcard?name=${encodeURIComponent(name)}&options=true`);
    const data = await res.json();
    const options = data.options || [];
    _optionsCache.set(name, options);
    return options;
  } catch {
    return [];
  }
}

// Read the current mode for a wildcard from node properties.
function getWildcardMode(node, name) {
  const modes = node.properties?.pcrWildcardModes;
  if (!modes || !modes[name]) return { mode: "randomize", index: 0 };
  return modes[name];
}

// Write a mode for a wildcard to node properties.
function setWildcardMode(node, name, mode, index, label) {
  if (!node.properties) node.properties = {};
  if (!node.properties.pcrWildcardModes) node.properties.pcrWildcardModes = {};

  // "randomize" is the default — remove the entry entirely to keep properties clean
  if (mode === "randomize") {
    delete node.properties.pcrWildcardModes[name];
    if (Object.keys(node.properties.pcrWildcardModes).length === 0) {
      delete node.properties.pcrWildcardModes;
    }
  } else {
    const entry = { mode, index: index || 0 };
    if (label) entry.label = label;
    node.properties.pcrWildcardModes[name] = entry;
  }
  app.graph?.setDirtyCanvas?.(true, true);
}

/**
 * Show the wildcard mode popup for a given wildcard name.
 *
 * @param {object} node - The ComfyUI graph node that contains the wildcard.
 * @param {string} wildcardName - The wildcard name (e.g. "gkr-wildcards/gkr-cyberpunk/archetype").
 * @param {DOMRect|object} triggerRect - Bounding rect of the clicked element.
 * @param {function} onChanged - Callback after mode/selection changes.
 */
export function showWildcardModePopup(node, wildcardName, triggerRect, onChanged) {
  const popupKey = `wc_${node.id}_${wildcardName}`;
  if (isPopupOpen(popupKey)) {
    closeActivePopup();
    return;
  }

  const current = getWildcardMode(node, wildcardName);
  const currentMode = current.mode;
  const currentIndex = current.index;

  const menu = document.createElement("div");
  menu.className = "pcr-mode-menu";

  let close;

  // -- mode section --
  const modeSection = document.createElement("div");
  modeSection.className = "pcr-mode-menu-modes";

  function createModeItem(label, mode) {
    const item = document.createElement("div");
    item.className = "pcr-mode-menu-item pcr-mode-menu-mode-option";
    if (currentMode === mode) item.classList.add("pcr-mode-menu-selected");

    const text = document.createElement("span");
    text.textContent = label;
    item.appendChild(text);

    if (currentMode === mode) {
      const check = document.createElement("span");
      check.className = "pcr-mode-menu-check";
      check.textContent = "\u2713";
      item.appendChild(check);
    }

    item.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      setWildcardMode(node, wildcardName, mode, 0);
      if (close) close();
      requestAnimationFrame(() => onChanged?.());
    });
    return item;
  }

  modeSection.appendChild(createModeItem(`${MODE_EMOJI.randomize} Randomize`, "randomize"));
  modeSection.appendChild(createModeItem(`${MODE_EMOJI.combine} Combine`, "combine"));
  modeSection.appendChild(createModeItem(`${MODE_EMOJI.iterate} Iterate`, "iterate"));
  modeSection.appendChild(createModeItem(`${MODE_EMOJI.none} None`, "none"));
  menu.appendChild(modeSection);

  // -- searchable options list (loaded async) --
  const loadingEl = document.createElement("div");
  loadingEl.className = "pcr-mode-menu-item";
  loadingEl.style.cssText = "color:#888;font-style:italic;";
  loadingEl.textContent = "Loading options\u2026";
  menu.appendChild(loadingEl);

  close = openPopup(menu, triggerRect, popupKey);

  // fetch options and replace loading indicator with searchable list
  fetchWildcardOptions(wildcardName).then((rawOptions) => {
    if (!menu.parentNode) return; // popup was closed before fetch completed

    loadingEl.remove();

    if (rawOptions.length === 0) return;

    const options = rawOptions.map((label, idx) => ({
      index: idx + 1,  // 1-based for switch mode
      label: label.replace(/:\d+\.?\d*\)/g, ")").replace(/\s+/g, " ").trim(),
      fullLabel: label,
    }));

    const list = createSearchableList({
      options,
      onSelect: (opt) => {
        setWildcardMode(node, wildcardName, "switch", opt.index, opt.label);
        if (close) close();
        requestAnimationFrame(() => onChanged?.());
      },
      currentMode: currentMode === "switch" ? "switch" : "",
      currentSwitchIndex: currentIndex,
    });

    if (list.searchContainer) menu.appendChild(list.searchContainer);
    if (list.separator) menu.appendChild(list.separator);
    menu.appendChild(list.listContainer);

    list.renderList();
    // reposition now that the menu has grown with options
    requestAnimationFrame(() => {
      positionPopup(menu, triggerRect);
      if (list.searchInput) list.searchInput.focus();
    });
  });
}

export { MODE_EMOJI, getWildcardMode, setWildcardMode };
