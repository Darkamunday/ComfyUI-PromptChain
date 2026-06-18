// History — client-side image history cache with subscribe pattern.
// Tracks generated images per workflow, backed by the /promptchain API.

import { api } from "../../../scripts/api.js";

const _cache = new Map();   // workflow_id → ImageEntry[]
const _subscribers = new Set();

// Prompt ids whose outputs are recorded by a dedicated tracker (background
// upscales). main.js's executed-event recorder skips these — its node-id maps
// were built from whatever graph was ACTIVE at execution_start, which is the
// user's workflow, not the background prompt's, so small-int node-id
// collisions would mis-record the output under the wrong workflow.
export const externallyTrackedPrompts = new Set();

// A background run's prompt id isn't known until its queue POST resolves, but
// the server can emit execution_start over the websocket BEFORE that lands.
// Without a guard the main-graph progress handler adopts the run and shows a
// node-panel spinner that never clears (the run's executed event is later
// skipped as external). A runner ARMS this window before queuing; the first
// unknown prompt seen by execution_start while it's open is claimed as external.
let _armedExternalQueues = 0;
export function armExternalQueue() { _armedExternalQueues++; }
export function disarmExternalQueue() { if (_armedExternalQueues > 0) _armedExternalQueues--; }
export function isExternalPrompt(promptId) {
  if (!promptId) return false;
  if (externallyTrackedPrompts.has(promptId)) return true;
  if (_armedExternalQueues > 0) {
    externallyTrackedPrompts.add(promptId);
    _armedExternalQueues--;
    return true;
  }
  return false;
}

function notify(workflowId) {
  for (const fn of _subscribers) {
    try { fn(workflowId); } catch {}
  }
}

export function subscribe(fn) {
  _subscribers.add(fn);
  return () => _subscribers.delete(fn);
}

export async function recordGeneration(workflowId, filename, subfolder, sourceType, metadata) {
  if (!workflowId || !filename) return null;
  try {
    const resp = await api.fetchApi(`/promptchain/generation/${encodeURIComponent(workflowId)}`, {
      method: "POST",
      body: JSON.stringify({
        filename,
        subfolder: subfolder || "",
        source_type: sourceType || "output",
        ...metadata,
      }),
      headers: { "Content-Type": "application/json" },
    });
    if (!resp.ok) return null;
    const entry = await resp.json();
    if (!_cache.has(workflowId)) _cache.set(workflowId, []);
    const list = _cache.get(workflowId);
    if (!list.some(e => e.hash === entry.hash)) list.unshift(entry);
    notify(workflowId);
    refreshEntryWorkflows(entry, workflowId);
    return entry;
  } catch {
    return null;
  }
}

// A derived image joins its parent's workflows server-side (record_image
// returns them as entry.workflows) — drop those caches and notify so any
// open gallery reloads with the family reordered. The window event carries
// the full entry so node preview panels can show externally tracked renders
// (viewer edit/inpaint/upscale), whose `executed` events main.js skips.
export function refreshEntryWorkflows(entry, recordedWorkflowId = null) {
  for (const wid of entry?.workflows || []) {
    if (wid === recordedWorkflowId) continue;
    _cache.delete(wid);
    notify(wid);
  }
  if (entry?.workflows?.length) {
    window.dispatchEvent(new CustomEvent("promptchain:generation-recorded", {
      detail: { entry, recordedWorkflowId },
    }));
  }
}

export async function fetchWorkflowImages(workflowId) {
  if (!workflowId) return [];
  if (_cache.has(workflowId)) return _cache.get(workflowId);
  try {
    const resp = await api.fetchApi(`/promptchain/workflow/${encodeURIComponent(workflowId)}?limit=10000`);
    if (!resp.ok) return [];
    const data = await resp.json();
    _cache.set(workflowId, data.images || []);
    return _cache.get(workflowId);
  } catch {
    return [];
  }
}

export function getCachedImages(workflowId) {
  return _cache.get(workflowId) || [];
}

export function invalidateCache(workflowId) {
  _cache.delete(workflowId);
}

export async function fetchWorkflowCount(workflowId) {
  if (!workflowId) return 0;
  if (_cache.has(workflowId)) return _cache.get(workflowId).length;
  try {
    const resp = await api.fetchApi(`/promptchain/count/${encodeURIComponent(workflowId)}`);
    if (!resp.ok) return 0;
    const data = await resp.json();
    return data.count || 0;
  } catch {
    return 0;
  }
}

export function thumbnailUrl(hash) {
  return api.apiURL(`/promptchain/thumb/${hash}`);
}

export async function checkOrphans(hashes) {
  if (!hashes?.length) return [];
  try {
    const resp = await api.fetchApi("/promptchain/check-orphans", {
      method: "POST",
      body: JSON.stringify({ hashes }),
      headers: { "Content-Type": "application/json" },
    });
    if (!resp.ok) return [];
    const data = await resp.json();
    return data.orphaned || [];
  } catch {
    return [];
  }
}
