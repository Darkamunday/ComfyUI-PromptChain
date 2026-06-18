// Workflow ID — read ComfyUI's native workflow UUID.
// UUID dedup on Save-As is handled server-side by _dedup_workflow_uuid.

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

export function getWorkflowId() {
  const graph = app.rootGraph ?? app.graph;
  return graph?.id || null;
}

// ── workflow id drift healing ────────────────────────────────────
// litegraph regenerates the graph uuid whenever a configure() receives state
// without a top-level `id` (clear() zeroes it, _configureBase mints a fresh
// one) — change-tracker restores and undo hit such paths. The generation
// panel keys history by that uuid, so each silent regeneration "empties" the
// panel and the next file save persists the drifted id.
//
// Detection is anchored to the id INSIDE the workflow's own saved content,
// AND gated on the configure belonging to the active workflow. The gate is
// load-bearing: ComfyUI's afterConfigureGraph hook fires inside loadGraphData
// BEFORE afterLoadNewGraph switches activeWorkflow, so during ANY tab
// open/switch the live graph already wears the INCOMING workflow's id while
// activeWorkflow still points at the OUTGOING one. Comparing that pair is
// indistinguishable from drift by content alone — it's how Save-As cloned
// the source file's history into the new file (sf-test-7 incident) despite
// the file-id anchoring. Undo/redo/reload pass their own workflow to
// loadGraphData (changeTracker.ts), so same-target loads are the only
// legitimate drift candidates; loads targeting another workflow, a string
// tab name (graft tabs), or nothing (PNG drags, templates) never heal.
const wfMeta = new WeakMap(); // ComfyWorkflow → { content, fileId, lastGraphId }

let currentLoad = null; // { targetPath } while app.loadGraphData is on the stack

export function installLoadTargetClassifier() {
  if (app.loadGraphData?.__pcrClassified) return;
  const orig = app.loadGraphData.bind(app);
  app.loadGraphData = async function (...args) {
    const wfArg = args[3];
    const prev = currentLoad;
    currentLoad = { targetPath: (wfArg && typeof wfArg === "object" && wfArg.path) || null };
    try {
      return await orig(...args);
    } finally {
      currentLoad = prev;
    }
  };
  app.loadGraphData.__pcrClassified = true;
}

export function healWorkflowIdDrift() {
  try {
    const graphId = getWorkflowId();
    const wf = app.extensionManager?.workflow?.activeWorkflow;
    if (!graphId || !wf) return;
    // Heal only when this configure reloaded the ACTIVE workflow itself.
    // Unknown provenance (classifier missed the load) skips too — a missed
    // heal costs one stale panel until the next reconfigure; a wrong clone
    // pollutes the DB and breaks the Save-As reset.
    if (!currentLoad || currentLoad.targetPath !== wf.path) return;
    let meta = wfMeta.get(wf);
    if (!meta || meta.content !== wf.content) {
      let fileId = null;
      try { fileId = JSON.parse(wf.content || wf.originalContent || "null")?.id || null; } catch { /* unparseable content — treat as no persisted identity */ }
      meta = { content: wf.content, fileId, lastGraphId: meta?.lastGraphId || null };
      wfMeta.set(wf, meta);
    }
    if (!meta.fileId) return;
    if (graphId === meta.fileId) { meta.lastGraphId = graphId; return; } // live matches the file — coherent
    // Live graph re-minted away from the file's identity. Heal from the file
    // id AND the previous drift segment (gens made between two drifts would
    // otherwise fall through), both idempotent server-side.
    const froms = [...new Set([meta.fileId, meta.lastGraphId])].filter((x) => x && x !== graphId);
    console.warn(`[PromptChain] workflow id drift (${wf.path}): file ${meta.fileId} vs live ${graphId} — re-attaching generation history`);
    for (const from of froms) {
      api.fetchApi("/promptchain/workflow-clone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_id: from, to_id: graphId, filepath: wf.path || "" }),
      }).catch((e) => console.warn("[PromptChain] id-drift history clone failed", e));
    }
    meta.lastGraphId = graphId;
  } catch (e) {
    console.warn("[PromptChain] id drift check failed", e);
  }
}

// Intercept workflow saves to detect new files (for sidebar refresh).
// NOTE — deliberate non-feature: Save-As to a NEW file mints a fresh uuid and
// the generation history intentionally RESETS with it (user spec: a new file
// starts a clean timeline). Only the SILENT id changes are healed, by
// healWorkflowIdDrift below — same tab path, regenerated uuid.
export function installSaveInterceptor() {
  installLoadTargetClassifier();
  const origFetch = api.fetchApi.bind(api);

  api.fetchApi = async function(route, options) {
    const result = await origFetch(route, options);

    if (options?.method === "POST" && typeof route === "string") {
      const decoded = decodeURIComponent(route);
      if (decoded.includes("/userdata/workflows/") && decoded.split("?")[0].endsWith(".json")) {
        const relPath = decoded.split("?")[0].split("/userdata/workflows/").pop();
        window.dispatchEvent(new CustomEvent("promptchain:workflows-changed", {
          detail: { path: relPath },
        }));
      }
    }

    return result;
  };
}
