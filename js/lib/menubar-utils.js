// menubar-utils.js — non-UI logic extracted from menubar.js.
// Mode setting, lock/disable cascade, overlay management, collapse.

import { app } from "../../../scripts/app.js";
import { getConnectedInputs, getLinkInfo } from "./slot-utils.js";
import { getSwitchOptions, countLabeledOptions } from "./label-utils.js";
import { updateParentLabels, updateInputLabels } from "./order-chain.js";
import { NODE_TYPE } from "./config.js";

// ── mode ─────────────────────────────────────────────────────────────────────

export function setModeOnNode(node, mode, switchIndex) {
  if (!node.properties) node.properties = {};
  node.properties.pcrMode = mode;
  if (switchIndex !== undefined) node.properties.pcrSwitchIndex = switchIndex;

  const modeWidget = node.widgets?.find(w => w.name === "mode");
  if (modeWidget) modeWidget.value = mode;
  if (switchIndex !== undefined) {
    const switchIndexWidget = node.widgets?.find(w => w.name === "switch_index");
    if (switchIndexWidget) switchIndexWidget.value = switchIndex;
  }

  for (const n of app.graph?._nodes || []) {
    if (n.comfyClass === NODE_TYPE) {
      try { updateInputLabels(n); }
      catch (e) { console.error("[PCR] error on node", n.id, n.title, e); }
    }
  }
  app.graph?.setDirtyCanvas?.(true, true);
}

// ── upstream traversal ───────────────────────────────────────────────────────

export function getUpstreamNodes(startNode, visited = new Set()) {
  const result = [];
  if (visited.has(startNode.id)) return result;
  visited.add(startNode.id);
  for (const input of startNode.inputs || []) {
    if (input.link == null) continue;
    const linkInfo = getLinkInfo(input.link);
    if (!linkInfo) continue;
    const source = app.graph?.getNodeById?.(linkInfo.origin_id);
    if (source?.comfyClass === "PromptChain_PromptChain") {
      result.push(source);
      result.push(...getUpstreamNodes(source, visited));
    }
  }
  return result;
}

// ── overlay (locked/disabled visual state) ───────────────────────────────────

const overlayObservers = new Map();

export function updateOverlay(targetNode) {
  const wantLocked = !!targetNode.properties?.pcrLocked && !targetNode.properties?.pcrDisabled;
  const wantDisabled = !!targetNode.properties?.pcrDisabled;

  const editorEl = targetNode.widgets?.find(w => w.name === "pcr_editor")?.element;
  const wrapper = editorEl?.parentElement;
  if (wrapper) {
    wrapper.style.background = (wantLocked || wantDisabled) ? "#0000004d" : "";
    wrapper.style.borderRadius = (wantLocked || wantDisabled) ? "4px" : "";
  }

  const nodeEl = document.querySelector(`[data-node-id="${targetNode.id}"]`);
  if (!nodeEl) return;

  function applyClasses() {
    nodeEl.classList.toggle("pcr-node-disabled", wantDisabled);
    nodeEl.classList.toggle("pcr-node-locked", wantLocked);
  }

  applyClasses();

  overlayObservers.get(targetNode.id)?.disconnect();

  if (wantLocked || wantDisabled) {
    const obs = new MutationObserver(() => applyClasses());
    obs.observe(nodeEl, { attributes: true, attributeFilter: ["class"] });
    overlayObservers.set(targetNode.id, obs);
  } else {
    overlayObservers.delete(targetNode.id);
  }
}

export function cleanupOverlayObserver(nodeId) {
  const obs = overlayObservers.get(nodeId);
  if (obs) {
    obs.disconnect();
    overlayObservers.delete(nodeId);
  }
}

// ── lock / disable cascade ───────────────────────────────────────────────────

function applyLock(targetNode, locked) {
  if (!targetNode.properties) targetNode.properties = {};
  targetNode.properties.pcrLocked = locked;
  if (locked && targetNode._pcrOutputText) {
    targetNode.properties.pcrCachedOutput = targetNode._pcrOutputText;
    targetNode.properties.pcrCachedNegOutput = targetNode._pcrNegOutputText || "";
    targetNode.properties.pcrCachedRegions = targetNode._pcrRegionsText || ""; // regional split freezes with the text
  }
  if (!locked) {
    targetNode.properties.pcrCachedOutput = "";
    targetNode.properties.pcrCachedNegOutput = "";
    targetNode.properties.pcrCachedRegions = "";
  }
}

export function handleLockCascade(node, locked) {
  const upstream = getUpstreamNodes(node);
  const allNodes = [node, ...upstream];
  for (const n of allNodes) {
    applyLock(n, locked);
    updateOverlay(n);
    if (n._pcrShared) {
      n._pcrShared.locked = locked;
    }
  }
  app.graph?.setDirtyCanvas?.(true, true);
}

export function handleDisableCascade(node, disabled) {
  const upstream = getUpstreamNodes(node);
  const allNodes = [node, ...upstream];
  for (const n of allNodes) {
    if (!n.properties) n.properties = {};
    n.properties.pcrDisabled = disabled;
    updateOverlay(n);
    if (n._pcrShared) {
      n._pcrShared.disabled = disabled;
    }
  }
  app.graph?.setDirtyCanvas?.(true, true);
}

// ── collapse / expand ────────────────────────────────────────────────────────

export function handleCollapse(node, collapsed, container) {
  if (!node.properties) node.properties = {};
  node.properties.pcrCollapsed = collapsed;

  const editorRow = container?.querySelector(".pcr-editor-row");
  const footer = container?.querySelector(".pcr-footer");
  const docSlot = container?.querySelector(".pcr-menubar-actions-left");
  const menubar = container?.querySelector(".pcr-menubar");
  const nodeEl = document.querySelector(`[data-node-id="${node.id}"]`);

  if (collapsed) {
    node.properties.pcrExpandedSize = [...node.size];
    if (editorRow) editorRow.style.display = "none";
    if (footer) footer.style.display = "none";
    if (docSlot) docSlot.style.visibility = "hidden";
    if (container) container.style.minHeight = "0";
    // close output panel before collapsing
    if (node._pcrOutputPanel?.getIsOpen?.()) {
      node._pcrOutputPanel.toggle();
    }
    const numInputs = (node.inputs || []).filter(inp => inp.name?.startsWith("in_")).length;
    const numOutputs = (node.outputs || []).length;
    const maxSlots = Math.max(numInputs, numOutputs);
    const collapsedHeight = 100 + (maxSlots * 24);
    node.setSize([node.size[0], collapsedHeight]);
    if (nodeEl) nodeEl.style.setProperty("--node-height", `${collapsedHeight}px`);
    if (menubar) { menubar.style.borderRadius = "4px"; menubar.style.background = "none"; }
  } else {
    if (editorRow) editorRow.style.display = "flex";
    if (footer) footer.style.display = "";
    if (docSlot) docSlot.style.visibility = "";
    if (container) container.style.minHeight = "";
    if (nodeEl) nodeEl.style.removeProperty("--node-height");
    if (menubar) { menubar.style.borderRadius = ""; menubar.style.background = ""; }
    const savedSize = node.properties.pcrExpandedSize;
    if (savedSize) {
      node.setSize(savedSize);
      delete node.properties.pcrExpandedSize;
      requestAnimationFrame(() => {
        node.setSize(savedSize);
        app.graph?.setDirtyCanvas?.(true, true);
      });
    }
  }
  app.graph?.setDirtyCanvas?.(true, true);
}

export function updateCollapsedHeight(node) {
  if (!node.properties?.pcrCollapsed) return;
  const numInputs = (node.inputs || []).filter(inp => inp.name?.startsWith("in_")).length;
  const numOutputs = (node.outputs || []).length;
  const maxSlots = Math.max(numInputs, numOutputs);
  const collapsedHeight = 100 + (maxSlots * 24);
  node.setSize([node.size[0], collapsedHeight]);
  const nodeEl = document.querySelector(`[data-node-id="${node.id}"]`);
  if (nodeEl) nodeEl.style.setProperty("--node-height", `${collapsedHeight}px`);
  app.graph?.setDirtyCanvas?.(true, true);
}
