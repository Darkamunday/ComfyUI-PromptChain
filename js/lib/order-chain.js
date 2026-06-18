// Order chain — builds and renders the hierarchical chain labels on input slots.
// Handles mode icons, child mode labels, chain walking, label updates, and
// wildcard conflict detection.

import { app } from "../../../scripts/app.js";
import { NODE_TYPE } from "./config.js";
import { isPromptChain, getSourceNode, hasCustomTitle, getConnectedInputs, hasOptions, getLinkInfo } from "./slot-utils.js";
import { attachSlotClickHandlers } from "./slot-dropdown.js";
import { countLabeledOptions, getSelfLabelOptions } from "./label-utils.js";

// Get the first-order mode icon based on the parent node's mode.
// Only shows when parent has 2+ connected inputs and isn't in switch mode.
function getModeIcon(node, slotIndex) {
  const mode = node.properties?.pcrMode || "switch";
  if (mode === "switch") {
    const switchIndex = node.properties?.pcrSwitchIndex ?? 1;
    if (slotIndex !== undefined) {
      return switchIndex === slotIndex ? "✅ " : "❌ ";
    }
    return switchIndex === 0 ? "❌ " : "✅ ";
  }
  const connectedCount = getConnectedInputs(node).length;
  if (connectedCount < 2) return "";
  if (mode === "combine") return "📚 ";
  if (mode === "roll") return "🎲 ";
  if (mode === "iterate") return "♻️ ";
  return "";
}

// Get the child node's mode label for display.
function getChildModeLabel(childNode) {
  const mode = childNode.properties?.pcrMode || "switch";
  if (mode === "roll") {
    const rollIdx = childNode.properties?.pcrRollSelected;
    if (rollIdx >= 1) {
      const inputs = getConnectedInputs(childNode);
      if (rollIdx <= inputs.length) {
        const source = getSourceNode(childNode, inputs[rollIdx - 1]);
        if (source) {
          const name = hasCustomTitle(source) ? source.title : "PromptChain";
          return `🎲 ${name}`;
        }
      }
      // fallback: inline ::Label:: options
      const selfLabels = getSelfLabelOptions(childNode);
      if (rollIdx <= selfLabels.length) {
        return `🎲 ${selfLabels[rollIdx - 1].label}`;
      }
    }
    return "🎲 Randomize";
  }
  if (mode === "combine") return "📚 Combine";
  if (mode === "switch") {
    const switchIndex = childNode.properties?.pcrSwitchIndex ?? 1;
    if (switchIndex === 0) return "❌ None";
    const childInputs = getConnectedInputs(childNode);
    if (switchIndex >= 1 && switchIndex <= childInputs.length) {
      const selectedSlot = childInputs[switchIndex - 1];
      const grandchild = getSourceNode(childNode, selectedSlot);
      if (grandchild) {
        const name = hasCustomTitle(grandchild) ? grandchild.title : "PromptChain";
        return `✅ ${name}`;
      }
    }
    // check self labels
    const promptWidget = childNode.widgets?.find(w => w.name === "prompt");
    const text = promptWidget?.value || "";
    const labelMatch = text.match(/^::([^:]+)::/m);
    if (labelMatch) {
      const lines = text.split("\n").filter(l => /^::([^:]+)::/.test(l.trim()));
      if (switchIndex >= 1 && switchIndex <= lines.length) {
        const selected = lines[switchIndex - 1].trim().match(/^::([^:]+)::/);
        if (selected) return `✅ ${selected[1]}`;
      }
    }
    return "🟢 Switch";
  }
  if (mode === "iterate") {
    const total = countLabeledOptions(childNode) || (childNode.properties?.pcrIterateTotal ?? 0);
    const cur = childNode.properties?.pcrIterateCurrent ?? 0;
    const cycle = childNode.properties?.pcrIterateCycle ?? 1;
    if (total > 0) {
      const display = `(${cur + 1}/${total})`;
      const cycleText = cycle > 1 ? ` x${cycle}` : "";
      return `♻️ Iterate ${display}${cycleText}`;
    }
    return "♻️ Iterate";
  }
  return "📚 Combine";
}

// Build order chain: walks the hierarchy from a child node downward.
// Returns array of { node, modeLabel, hasOptions } for each level.
export function buildOrderChain(childNode, maxDepth = 10) {
  const chain = [];
  let current = childNode;

  for (let depth = 0; depth < maxDepth && current; depth++) {
    if (!hasOptions(current)) break;

    const modeLabel = getChildModeLabel(current);
    chain.push({ node: current, modeLabel, hasOptions: true });

    // walk to selected child if in switch mode
    const mode = current.properties?.pcrMode || "switch";
    if (mode === "switch") {
      const switchIndex = current.properties?.pcrSwitchIndex ?? 1;
      const inputs = getConnectedInputs(current);
      if (switchIndex >= 1 && switchIndex <= inputs.length) {
        const nextNode = getSourceNode(current, inputs[switchIndex - 1]);
        if (nextNode && isPromptChain(nextNode)) {
          current = nextNode;
          continue;
        }
      }
    }
    break;
  }
  return chain;
}

// Update input slot labels with full order chain.
export function updateInputLabels(node) {
  if (!node.inputs) return;

  for (let i = 0; i < node.inputs.length; i++) {
    const slot = node.inputs[i];
    if (!slot.name?.includes("in_")) continue;

    if (slot.link == null) {
      slot.label = "in";
      slot.localized_name = "in";
    } else {
      const source = getSourceNode(node, slot);
      if (source) {
        const slotName = slot.name.replace("inputs.", "");
        const slotIndex = parseInt(slotName.split("_")[1]) + 1;
        const modeIcon = getModeIcon(node, slotIndex);
        const name = hasCustomTitle(source) ? source.title : "PromptChain";
        const chain = buildOrderChain(source);

        if (chain.length > 0) {
          const chainStr = chain.map(c => c.modeLabel).join("  ");
          slot.label = `${modeIcon}${name}   ${chainStr}`;
        } else {
          slot.label = `${modeIcon}${name}`;
        }
        slot.localized_name = slot.label;

        // store chain data for click handlers
        slot._pcrOrderChain = chain;
        slot._pcrSourceNode = source;
      }
    }
    // New object reference so Vue detects the label change — but skip
    // during active link drags to preserve slot identity for connectSlots.
    if (!app.canvas?.linkConnector?.isConnecting) {
      node.inputs[i] = { ...slot };
    }
  }
  // notify Vue reactivity that slot labels changed
  // node:slot-label:changed triggers shallowReactive array re-splice in Vue
  node.graph?.trigger?.("node:slot-label:changed", {
    nodeId: node.id, slotType: 1,
  });
  app.canvas?.setDirty(true, true);
  // re-attach click handlers after Vue re-renders the label DOM
  requestAnimationFrame(() => attachSlotClickHandlers(node));
}

// Update labels on all parent nodes connected to this node's outputs, recursively.
export function updateParentLabels(childNode) {
  if (!childNode.outputs) return;
  for (const output of childNode.outputs) {
    if (!output.links) continue;
    for (const linkId of output.links) {
      const link = getLinkInfo(linkId);
      if (!link) continue;
      const parentNode = app.graph?.getNodeById?.(link.target_id);
      if (parentNode?.comfyClass === NODE_TYPE) {
        updateInputLabels(parentNode);
        updateParentLabels(parentNode);
      }
    }
  }
}

// Check for inline wildcard + child input conflict.
// When both exist, inline ::Label:: lines are ignored (children take priority).
// Visual warning: adds a CSS class that the linter/style can use to highlight labels.
const toastShown = new Set();
export function checkWildcardConflict(node) {
  // read from CM editor when available (survives widget value corruption/timing)
  const text = node._pcrEditor?.state.doc.toString()
    || node.widgets?.find(w => w.name === "prompt")?.value
    || "";
  const hasLabels = /^::([^:]+)::/m.test(text);
  const hasChildren = getConnectedInputs(node).length > 0;

  const conflict = hasLabels && hasChildren;
  node._pcrWildcardConflict = conflict;

  // dim via CSS class on the container — try live DOM first (after undo,
  // node's container may be detached even with widget.onRemove cleanup)
  const container = document.querySelector(`[data-node-id="${node.id}"] .pcr-editor`)
    || node._pcrEditor?.dom?.closest(".pcr-editor");
  if (container) container.classList.toggle("pcr-wildcard-conflict", conflict);

  // toast once per node per conflict
  if (conflict && !toastShown.has(node.id)) {
    toastShown.add(node.id);
    if (app.extensionManager?.toast?.add) {
      app.extensionManager.toast.add({
        severity: "info",
        summary: "PromptChain",
        detail: "Inline wildcards dimmed — child inputs take priority",
        life: 3000,
      });
    } else {
      console.warn("[PromptChain] Inline wildcards ignored — child inputs take priority");
    }
  } else if (!conflict) {
    toastShown.delete(node.id);
  }
}

export function cleanupOrderChain(nodeId) {
  toastShown.delete(nodeId);
}
