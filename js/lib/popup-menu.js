// Popup menu — shared searchable dropdown used by menubar and slot-dropdown.
// Handles positioning, search filtering, keyboard navigation, and close behavior.

let activePopup = null;
let activePopupTarget = null;
let popupOpenedAt = 0;

function dismissActivePopup() {
  if (Date.now() - popupOpenedAt < 300) return;
  closeActivePopup();
}
document.addEventListener("click", dismissActivePopup);
// LiteGraph canvas captures pointer events, preventing click from reaching
// document — listen on pointerdown too so clicking the canvas closes popups
document.addEventListener("pointerdown", dismissActivePopup);

export function closeActivePopup() {
  if (activePopup) { activePopup(); activePopup = null; }
  activePopupTarget = null;
}

export function isPopupOpen(key) {
  return activePopup && activePopupTarget === key;
}

// Highlight search term matches within text. Returns a DocumentFragment.
export function highlightMatch(text, terms) {
  if (!terms.length) return document.createTextNode(text);
  const fragment = document.createDocumentFragment();
  let remaining = text;
  let lower = remaining.toLowerCase();

  while (remaining.length > 0) {
    let earliest = -1;
    let matched = "";
    for (const term of terms) {
      const idx = lower.indexOf(term);
      if (idx !== -1 && (earliest === -1 || idx < earliest)) {
        earliest = idx;
        matched = term;
      }
    }
    if (earliest === -1) {
      fragment.appendChild(document.createTextNode(remaining));
      break;
    }
    if (earliest > 0) {
      fragment.appendChild(document.createTextNode(remaining.slice(0, earliest)));
    }
    const hl = document.createElement("span");
    hl.className = "pcr-mode-menu-highlight";
    hl.textContent = remaining.slice(earliest, earliest + matched.length);
    fragment.appendChild(hl);
    remaining = remaining.slice(earliest + matched.length);
    lower = remaining.toLowerCase();
  }
  return fragment;
}

// Position a popup menu below a trigger rect, clamped to viewport.
export function positionPopup(menu, triggerRect) {
  const menuRect = menu.getBoundingClientRect();
  let left = triggerRect.left;
  let top = triggerRect.bottom + 4;
  if (left + menuRect.width > window.innerWidth) left = window.innerWidth - menuRect.width - 10;
  if (top + menuRect.height > window.innerHeight) top = triggerRect.top - menuRect.height - 4;
  if (left < 10) left = 10;
  if (top < 10) top = 10;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

// Creates a searchable, keyboard-navigable option list inside a container.
// Returns { renderList, listContainer } for the caller to append.
//
// options: [{ index, label, ... }]
// onSelect: (option) => void
// currentMode: string
// currentSwitchIndex: number
// itemPrefix: string — optional prefix per label (e.g. emoji)
// extraItemSetup: (item, option) => void — optional per-item customization
export function createSearchableList({
  options,
  onSelect,
  currentMode,
  currentSwitchIndex,
  itemPrefix = "",
  extraItemSetup = null,
}) {
  const listContainer = document.createElement("div");
  listContainer.className = "pcr-mode-menu-list";

  let searchInput = null;
  let searchContainer = null;
  let separator = null;
  let selectedIndex = -1;
  let visibleItems = [];

  if (options.length > 0) {
    searchContainer = document.createElement("div");
    searchContainer.className = "pcr-mode-menu-search-container";
    searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.className = "pcr-mode-menu-search";
    searchInput.placeholder = "Search options...";
    searchContainer.appendChild(searchInput);

    separator = document.createElement("div");
    separator.className = "pcr-mode-menu-separator";
  }

  function updateKeyboardSelection(newIndex) {
    if (selectedIndex >= 0 && selectedIndex < visibleItems.length) {
      visibleItems[selectedIndex].element.classList.remove("pcr-mode-menu-keyboard-selected");
    }
    selectedIndex = newIndex;
    if (selectedIndex >= 0 && selectedIndex < visibleItems.length) {
      const item = visibleItems[selectedIndex].element;
      item.classList.add("pcr-mode-menu-keyboard-selected");
      const listRect = listContainer.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      if (itemRect.top < listRect.top) item.scrollIntoView({ block: "start", behavior: "instant" });
      else if (itemRect.bottom > listRect.bottom) item.scrollIntoView({ block: "end", behavior: "instant" });
    }
  }

  function renderList(filter = "") {
    listContainer.innerHTML = "";
    visibleItems = [];
    selectedIndex = -1;
    if (options.length === 0) return;

    const terms = filter.toLowerCase().split(/\s+/).filter(t => t.length > 0);
    const filtered = options.filter(opt =>
      !terms.length || terms.every(t => opt.label.toLowerCase().includes(t))
    );

    if (filtered.length === 0) {
      const empty = document.createElement("div");
      empty.className = "pcr-mode-menu-empty";
      empty.textContent = "No matching options";
      listContainer.appendChild(empty);
      return;
    }

    filtered.forEach((opt, idx) => {
      const item = document.createElement("div");
      item.className = "pcr-mode-menu-item";
      const isSelected = currentMode === "switch" && opt.index === currentSwitchIndex;
      if (isSelected) item.classList.add("pcr-mode-menu-selected");

      const labelSpan = document.createElement("span");
      labelSpan.className = "pcr-mode-menu-label";
      labelSpan.appendChild(highlightMatch(`${itemPrefix}${opt.label}`, terms));
      item.appendChild(labelSpan);

      if (isSelected) {
        const check = document.createElement("span");
        check.className = "pcr-mode-menu-check";
        check.textContent = "\u2713";
        item.appendChild(check);
      }

      extraItemSetup?.(item, opt);

      item.addEventListener("click", (e) => {
        e.stopPropagation();
        onSelect(opt);
      });
      item.addEventListener("mouseenter", () => updateKeyboardSelection(idx));

      listContainer.appendChild(item);
      visibleItems.push({ element: item, option: opt });
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", () => renderList(searchInput.value));
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (visibleItems.length > 0) updateKeyboardSelection((selectedIndex + 1) % visibleItems.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (visibleItems.length > 0) updateKeyboardSelection(selectedIndex > 0 ? selectedIndex - 1 : visibleItems.length - 1);
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (selectedIndex >= 0 && selectedIndex < visibleItems.length) {
          visibleItems[selectedIndex].element.click();
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeActivePopup();
      }
      e.stopPropagation();
    });
  }

  return {
    listContainer,
    searchContainer,
    separator,
    searchInput,
    renderList,
  };
}

// Open a popup menu, managing the global singleton lifecycle.
// Returns a close() function.
export function openPopup(menu, triggerRect, popupKey) {
  // close any existing popup
  closeActivePopup();
  document.querySelectorAll(".pcr-mode-menu").forEach(el => el.remove());

  menu.addEventListener("click", (e) => e.stopPropagation());
  menu.addEventListener("pointerdown", (e) => e.stopPropagation());

  function close() {
    if (menu.parentNode) menu.remove();
    if (activePopup === close) activePopup = null;
    activePopupTarget = null;
  }

  document.body.appendChild(menu);
  positionPopup(menu, triggerRect);

  activePopup = close;
  activePopupTarget = popupKey || null;
  popupOpenedAt = Date.now();

  return close;
}
