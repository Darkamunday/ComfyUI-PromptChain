import { app } from "../../../scripts/app.js";
import { NODE_TYPE } from "./config.js";

export function isPromptChain(node) {
  return node?.comfyClass === NODE_TYPE ||
         node?.constructor?.comfyClass === NODE_TYPE ||
         node?.type === NODE_TYPE;
}

export function hasCustomTitle(node) {
  if (!node.title) return false;
  const defaultTitle = node.constructor?.title || node.type;
  return node.title !== defaultTitle &&
         node.title !== node.comfyClass &&
         node.title !== node.constructor?.comfyClass;
}

export function getLinkInfo(linkId) {
  return app.graph?.links?.get?.(linkId) || app.graph?.links?.[linkId] || null;
}

export function getLink(graph, linkId) {
  return graph?.links?.get?.(linkId) || graph?.links?.[linkId] || null;
}

export function getSourceNode(node, slot) {
  if (slot.link === null) return null;
  const link = getLinkInfo(slot.link);
  if (!link) return null;
  return app.graph?.getNodeById?.(link.origin_id) || null;
}

// Handles both 1.0 ("in_N") and 2.0 ("inputs.in_N") slot name patterns.
export function getConnectedInputs(node) {
  if (!node?.inputs) return [];
  return node.inputs.filter(slot =>
    (slot.name?.startsWith("in_") || slot.name?.startsWith("inputs.in_")) && slot.link != null
  );
}

export function hasOptions(node) {
  if (!node) return false;
  if (getConnectedInputs(node).length > 0) return true;
  const promptWidget = node.widgets?.find(w => w.name === "prompt");
  return /^::([^:]+)::/m.test(promptWidget?.value || "");
}
