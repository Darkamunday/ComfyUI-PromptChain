// Sidebar shared state — selection, navigation, and view preferences.

export const VIEW_MODES = [
  { id: "grid", label: "Grid" },
  { id: "justified", label: "Justified" },
  { id: "list", label: "List" },
];

export const SORT_FIELDS = [
  { field: "name", label: "Name" },
  { field: "modified", label: "Date" },
  { field: "size", label: "Size" },
  { field: "type", label: "Type" },
];

export const GROUP_MODES = [
  { id: "none", label: "None" },
  { id: "time", label: "Time" },
  { id: "type", label: "Type" },
  { id: "size", label: "Size" },
];

const PREFIX = "pcr-sidebar-";

function load(key, fallback) {
  try {
    const v = localStorage.getItem(PREFIX + key);
    return v !== null ? JSON.parse(v) : fallback;
  } catch { return fallback; }
}

function save(key, val) {
  try { localStorage.setItem(PREFIX + key, JSON.stringify(val)); } catch {}
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

export const selection = $state({ items: new Set(), anchor: null });

export function selectItem(path) {
  selection.items = new Set([path]);
  selection.anchor = path;
}

export function toggleItem(path) {
  const next = new Set(selection.items);
  next.has(path) ? next.delete(path) : next.add(path);
  selection.items = next;
  if (next.has(path)) selection.anchor = path;
}

export function selectRange(path, allPaths) {
  if (!selection.anchor) return selectItem(path);
  const ai = allPaths.indexOf(selection.anchor);
  const ti = allPaths.indexOf(path);
  if (ai < 0 || ti < 0) return selectItem(path);
  selection.items = new Set(allPaths.slice(Math.min(ai, ti), Math.max(ai, ti) + 1));
}

export function clearSelection() {
  selection.items = new Set();
  selection.anchor = null;
}

export function selectAll(allPaths) {
  selection.items = new Set(allPaths);
  selection.anchor = allPaths[0] ?? null;
}

export function setSelection(paths) {
  selection.items = new Set(paths);
  selection.anchor = paths[0] ?? null;
}

// ---------------------------------------------------------------------------
// Navigation (localStorage-persisted — survives page refresh)
// ---------------------------------------------------------------------------

export const nav = $state({
  scope: load("navScope", "workflows"),
  paths: load("navPaths", { workflows: [], input: [], output: [] }),
});

// auto-persist nav changes
const _cleanupNav = $effect.root(() => {
  $effect(() => { save("navScope", nav.scope); });
  $effect(() => { save("navPaths", { ...nav.paths }); });
});

// ---------------------------------------------------------------------------
// View Preferences (localStorage-persisted)
// ---------------------------------------------------------------------------

// migrate from old single viewMode to per-scope viewModes
function loadViewModes() {
  const raw = load("viewModes", null);
  if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw;
  // first-install defaults: list for workflows, grid for input, justified for output
  const old = load("viewMode", null);
  const modes = old
    ? { workflows: old, input: old, output: old }
    : { workflows: "list", input: "grid", output: "justified" };
  save("viewModes", modes);
  return modes;
}

function loadThumbSizes() {
  const raw = load("thumbSizes", null);
  if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw;
  const old = load("thumbSize", 140);
  const sizes = { workflows: old, input: old, output: old };
  save("thumbSizes", sizes);
  return sizes;
}

export const prefs = $state({
  viewModes: loadViewModes(),
  thumbSizes: loadThumbSizes(),
  sortField: load("sortField", "modified"),
  sortDirection: load("sortDir", "desc"),
  groupModes: load("groupModes", { workflows: "none", input: "none", output: "none" }),
  feedModes: load("feedModes", { workflows: false, input: false, output: false }),
  favFilters: load("favFilters", { workflows: false, input: false, output: false }),
});

// per-scope accessors
export function viewMode() {
  return prefs.viewModes[nav.scope] || "grid";
}

export function thumbSize() {
  return prefs.thumbSizes[nav.scope] || 140;
}

export function setViewMode(m) {
  prefs.viewModes[nav.scope] = m;
  save("viewModes", { ...prefs.viewModes });
}

export function setThumbSize(size) {
  prefs.thumbSizes[nav.scope] = Math.max(80, Math.min(300, Math.round(size)));
  save("thumbSizes", { ...prefs.thumbSizes });
}

export function setSort(field, direction) {
  prefs.sortField = field;
  prefs.sortDirection = direction;
  save("sortField", field);
  save("sortDir", direction);
}

// ---------------------------------------------------------------------------
// Focused item cursor (session-only, for arrow-key navigation)
// ---------------------------------------------------------------------------

export const cursor = $state({ path: null });

export function setCursor(path) { cursor.path = path; }
export function clearCursor() { cursor.path = null; }

// ---------------------------------------------------------------------------
// Grouping preference (localStorage-persisted)
// ---------------------------------------------------------------------------

export function groupMode() {
  return prefs.groupModes?.[nav.scope] || "none";
}

export function setGroupMode(mode) {
  if (!prefs.groupModes) prefs.groupModes = { workflows: "none", input: "none", output: "none" };
  prefs.groupModes[nav.scope] = mode;
  save("groupModes", { ...prefs.groupModes });
}

// ---------------------------------------------------------------------------
// Recent-feed mode (localStorage-persisted) — flat newest-first subtree
// listing instead of folder browsing.
// ---------------------------------------------------------------------------

export function feedMode() {
  return !!prefs.feedModes?.[nav.scope];
}

export function setFeedMode(on) {
  if (!prefs.feedModes) prefs.feedModes = { workflows: false, input: false, output: false };
  prefs.feedModes[nav.scope] = !!on;
  save("feedModes", { ...prefs.feedModes });
}

// ---------------------------------------------------------------------------
// Starred-only filter (localStorage-persisted, per scope)
// ---------------------------------------------------------------------------

export function favFilter() {
  return !!prefs.favFilters?.[nav.scope];
}

export function setFavFilter(on) {
  if (!prefs.favFilters) prefs.favFilters = { workflows: false, input: false, output: false };
  prefs.favFilters[nav.scope] = !!on;
  save("favFilters", { ...prefs.favFilters });
}

// ---------------------------------------------------------------------------
// Clipboard (session-only)
// ---------------------------------------------------------------------------

export const clipboard = $state({
  items: [],       // [{ path, name, type }]
  scope: null,     // source scope
  op: null,        // "cut" | "copy"
});

export function clipCut(scope, items) {
  clipboard.items = items.map(i => ({ path: i.path, name: i.name, type: i.type }));
  clipboard.scope = scope;
  clipboard.op = "cut";
}

export function clipCopy(scope, items) {
  clipboard.items = items.map(i => ({ path: i.path, name: i.name, type: i.type }));
  clipboard.scope = scope;
  clipboard.op = "copy";
}

export function clipClear() {
  clipboard.items = [];
  clipboard.scope = null;
  clipboard.op = null;
}
