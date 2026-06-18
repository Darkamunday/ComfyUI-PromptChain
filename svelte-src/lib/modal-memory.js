// Per-image session memory for the inpaint/upscale modals: reopening a modal
// on the same image restores the last-applied prompt/engine/dials instead of
// resetting to defaults (iterating is the whole point of these modals).
// localStorage, LRU-capped — best-effort, never load-bearing.

const KEY = "pcr.modal.memory";
const MAX_ENTRIES = 60;

function readAll() {
  try {
    return JSON.parse(localStorage.getItem(KEY)) || {};
  } catch {
    return {};
  }
}

export function recallModalMemory(kind, imageKey) {
  if (!imageKey) return null;
  return readAll()[`${kind}:${imageKey}`] || null;
}

export function storeModalMemory(kind, imageKey, value) {
  if (!imageKey) return;
  try {
    const all = readAll();
    all[`${kind}:${imageKey}`] = { ...value, t: Date.now() };
    const keys = Object.keys(all);
    if (keys.length > MAX_ENTRIES) {
      keys.sort((a, b) => (all[a]?.t || 0) - (all[b]?.t || 0));
      for (const k of keys.slice(0, keys.length - MAX_ENTRIES)) delete all[k];
    }
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch { /* storage blocked/full */ }
}
