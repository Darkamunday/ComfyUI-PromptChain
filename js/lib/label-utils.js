// Label utilities — parse ::Label:: syntax from prompt content.
// Used by the chaining system to count options for iterate/switch modes.

import { getConnectedInputs, getSourceNode, hasCustomTitle } from "./slot-utils.js";

// Returns the prompt text from a node. Prefers CM editor content (always current),
// falls back to widget value, then stored document content.
export function getNodePromptContent(node) {
  // CM editor is the most reliable source (survives widget value corruption)
  if (node?._pcrEditor) return node._pcrEditor.state.doc.toString();
  const promptWidget = node?.widgets?.find(w => w.name === "prompt");
  if (promptWidget?.value) return promptWidget.value;
  if (node?.properties?.pcrDocuments) {
    const activeDoc = node.properties.pcrDocuments.find(
      d => d.id === node.properties.pcrActiveDocId
    );
    if (activeDoc?.content) return activeDoc.content;
  }
  return "";
}

// Counts iterate options: connected children take priority over inline-wildcards.
// Auto-resets iterate state if the current index exceeds the option count.
export function countLabeledOptions(node) {
  let count = 0;

  // children override inline-wildcards
  const connected = getConnectedInputs(node).length;
  if (connected > 0) {
    count = connected;
  } else {
    const content = getNodePromptContent(node);
    if (content) {
      for (const line of content.split("\n")) {
        if (/^::([^:]+)::/.test(line.trim())) count++;
      }
    }
  }

  // auto-reset stale iterate index
  if (count > 0 && node?.properties) {
    const currentIndex = node.properties.pcrIterateIndex ?? 0;
    if (currentIndex >= count) {
      node.properties.pcrIterateIndex = 0;
      node.properties.pcrIterateCycle = 1;
      syncWidgetValues(node);
    }
  }

  return count;
}

// Returns ::Label:: entries as [{index: 1-based, label: string}] for switch menus.
export function getSelfLabelOptions(node) {
  const content = getNodePromptContent(node);
  if (!content) return [];

  const options = [];
  let index = 1; // 1-based for switch mode
  for (const line of content.split("\n")) {
    const match = line.trim().match(/^::([^:]+)::/);
    if (match) {
      options.push({ index, label: match[1] });
      index++;
    }
  }
  return options;
}

// Returns switch options: connected children (with source node refs) + self-label fallback.
// Used by menubar and slot dropdown to populate the mode selection menu.
export function getSwitchOptions(node) {
  const options = [];

  for (const slot of getConnectedInputs(node)) {
    const source = getSourceNode(node, slot);
    if (!source) continue;
    const name = hasCustomTitle(source) ? source.title : "PromptChain";
    const slotName = slot.name.replace("inputs.", "");
    const index = parseInt(slotName.split("_")[1]) + 1;
    options.push({ index, label: name, sourceNode: source });
  }

  if (options.length === 0) {
    for (const opt of getSelfLabelOptions(node)) {
      options.push({ index: opt.index, label: opt.label, sourceNode: null });
    }
  }

  return options;
}

// Syncs node.properties to hidden widget values (ComfyUI reads widgets at execution).
function syncWidgetValues(node) {
  if (!node?.widgets) return;
  for (const widget of node.widgets) {
    const propKey = {
      mode: "pcrMode",
      switch_index: "pcrSwitchIndex",
      iterate_index: "pcrIterateIndex",
      iterate_cycle: "pcrIterateCycle",
      locked: "pcrLocked",
      disabled: "pcrDisabled",
    }[widget.name];
    if (propKey && node.properties?.[propKey] !== undefined) {
      widget.value = node.properties[propKey];
    }
  }
}
