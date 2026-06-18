/**
 * Workflow Blank State
 *
 * When Comfy.Workflow.Persist is off the user wants a clean saveable tab on
 * startup, not the sample template the frontend now auto-loads.  We replace
 * three template-load entry points with a blank graph by intercepting
 * app.loadGraphData (the single funnel they all go through):
 *
 *   1. Startup — initializeWorkflow → loadDefaultWorkflow → loadGraphData()
 *      with no graphData; app.loadGraphData would substitute defaultGraph.
 *   2. Closing the last tab — workflowService.closeWorkflow calls its own
 *      loadDefaultWorkflow which passes defaultGraph explicitly.
 *   3. Opening a new workflow while the current temporary tab is still empty
 *      — creates a redundant tab instead of replacing the empty one.
 *
 * Install timing matters: this module is statically imported and the patch
 * is applied at top-level so it lands before Vue mounts GraphCanvas (whose
 * onMounted triggers initializeWorkflow).  No polling, no microtasks.
 */

import { app } from "../../../../scripts/app.js";

const BLANK_GRAPH = Object.freeze({
    last_node_id: 0, last_link_id: 0,
    nodes: [], links: [], groups: [],
    config: {}, extra: {}, version: 0.4,
});

let _installed = false;

function persistOff() {
    return !app.extensionManager?.setting?.get("Comfy.Workflow.Persist");
}

function findEmptyTempTab(store) {
    const active = store?.activeWorkflow;
    if (!active?.isTemporary) return null;
    if (app.graph?._nodes?.length) return null;
    return active;
}

function looksLikeDefaultTemplate(graphData) {
    // defaultGraph has the canonical SDXL nodes; any explicit nodes array
    // arriving via the close-last-tab path is the template by definition,
    // since that path can't carry user data.
    return Array.isArray(graphData?.nodes) && graphData.nodes.length > 0;
}

// Wrap workflowStore.closeWorkflow so we get a direct "close completed"
// signal — Pinia $subscribe doesn't surface mutations on the store's
// internal openWorkflowPaths/workflowLookup refs since they aren't
// returned from the setup function.  After each close, if the active
// tab carries a "(N).json" suffix and the unsuffixed name is now free,
// rename it.  Event-driven (post-close hook), installed once per store.
let _closePatched = false;
function patchStoreCloseForRename(store) {
    if (_closePatched || !store || typeof store.closeWorkflow !== "function") return;
    _closePatched = true;
    const originalClose = store.closeWorkflow.bind(store);
    store.closeWorkflow = async function patchedClose(workflow) {
        const result = await originalClose(workflow);
        const active = store.activeWorkflow;
        if (active?.isTemporary && /\(\d+\)\.json$/.test(active.path)) {
            const cleanPath = active.path.replace(/\s*\(\d+\)(\.json)$/, "$1");
            const collision = store.workflows?.some?.(w => w.path === cleanPath);
            if (!collision) {
                store.renameWorkflow(active, cleanPath);
            }
        }
        return result;
    };
}

export function installWorkflowBlankStateFix() {
    if (_installed) return;
    if (!app.loadGraphData) {
        queueMicrotask(installWorkflowBlankStateFix);
        return;
    }
    _installed = true;

    const original = app.loadGraphData.bind(app);

    app.loadGraphData = async function patched(graphData, clean, restore_view, workflow, options) {
        const store = app.extensionManager?.workflow;

        // Implicit default-template load (entry point #1).
        // initializeWorkflow → loadDefaultWorkflow → loadGraphData() routes
        // here with no graphData; the unpatched function would substitute
        // defaultGraph internally.  Substitute BLANK_GRAPH instead.
        if (persistOff() && !workflow && graphData == null) {
            return original(BLANK_GRAPH, clean, restore_view, workflow, options);
        }

        // Last-tab-close default-template load (entry point #2).
        // workflowService.closeWorkflow calls loadDefaultWorkflow BEFORE the
        // old tab is removed from the store, so the new tab inherits an
        // "Unsaved Workflow (2).json" suffix from getUnconflictedPath.
        // Renaming here would collide with the still-present old path and
        // cause both entries to be purged when workflowStore.closeWorkflow
        // runs.  Subscribe to the store and rename only once the old path
        // has actually left openWorkflows.
        if (
            persistOff() &&
            !workflow &&
            !clean &&
            looksLikeDefaultTemplate(graphData) &&
            store?.openWorkflows?.length <= 1
        ) {
            patchStoreCloseForRename(store);
            return original(BLANK_GRAPH, clean, restore_view, workflow, options);
        }

        // Redundant-tab cleanup (entry point #3).
        // Loading a new workflow while sitting on an empty temporary tab
        // leaves the empty tab orphaned.  Snapshot it before the load, close
        // it after the new workflow becomes active.
        const orphanedBlank = (looksLikeDefaultTemplate(graphData) || workflow) ? findEmptyTempTab(store) : null;

        const result = await original(graphData, clean, restore_view, workflow, options);

        if (orphanedBlank && store?.openWorkflows?.length > 1) {
            const stillOpen = store.openWorkflows.find(w => w.path === orphanedBlank.path);
            if (stillOpen && !store.isActive(stillOpen)) {
                store.closeWorkflow(stillOpen);
            }
        }

        return result;
    };
}
