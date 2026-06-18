// Shared reactive state for popup management and model recognition.
// Module-level $state is reactive across all Svelte consumers.

// popup singleton — only one popup open at a time
export const popup = $state({
  activeKey: null,
  close: null,
});

export function closeActivePopup() {
  popup.close?.();
  popup.activeKey = null;
  popup.close = null;
}

export function openPopupState(key, closeFn) {
  closeActivePopup();
  popup.activeKey = key;
  popup.close = closeFn;
}

// model recognition progress (pushed via WebSocket from backend)
export const recognition = $state({
  running: false,
  total: 0,
  done: 0,
});
