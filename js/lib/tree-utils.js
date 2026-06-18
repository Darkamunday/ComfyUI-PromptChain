// Tree utilities — pure functions for building the PromptChain network tree.
// Extracted from fullscreen-editor.js for use by both imperative and Svelte code.
// These depend on graph state (app, slot-utils, label-utils) so they stay in js/lib/.

import { app } from "../../../scripts/app.js";
import { isPromptChain, getSourceNode, getConnectedInputs, hasCustomTitle, getLinkInfo } from "./slot-utils.js";
import { getSelfLabelOptions, getNodePromptContent } from "./label-utils.js";

export function findRoot(node) {
  let current = node;
  const visited = new Set();
  while (current) {
    if (visited.has(current.id)) break;
    visited.add(current.id);
    let parent = null;
    for (const output of current.outputs || []) {
      for (const linkId of output.links || []) {
        const link = getLinkInfo(linkId);
        if (!link) continue;
        const target = app.graph?.getNodeById?.(link.target_id);
        if (target && isPromptChain(target)) {
          parent = target;
          break;
        }
      }
      if (parent) break;
    }
    if (!parent) break;
    current = parent;
  }
  return current;
}

export function buildTree(node, ancestors = new Set()) {
  if (!node || ancestors.has(node.id)) return null;
  ancestors.add(node.id);

  const children = [];
  const inputs = getConnectedInputs(node);
  inputs.sort((a, b) => {
    const ai = parseInt(a.name.replace("inputs.", "").split("_")[1]);
    const bi = parseInt(b.name.replace("inputs.", "").split("_")[1]);
    return ai - bi;
  });

  for (const slot of inputs) {
    const source = getSourceNode(node, slot);
    if (source && isPromptChain(source)) {
      const child = buildTree(source, ancestors);
      if (child) children.push(child);
    }
  }

  ancestors.delete(node.id);

  const labels = children.length === 0 ? getSelfLabelOptions(node) : [];

  const wildcards = [];
  if (children.length === 0) {
    const content = getNodePromptContent(node);
    if (content) {
      const wcRegex = /__([a-zA-Z0-9_/.-]+)__/g;
      const wcModes = node.properties?.pcrWildcardModes || {};
      const wcResults = node.properties?.pcrWildcardResults || {};
      const seen = new Set();
      let m;
      while ((m = wcRegex.exec(content)) !== null) {
        const name = m[1];
        if (!seen.has(name)) {
          seen.add(name);
          const wm = wcModes[name] || {};
          const wr = wcResults[name] || {};
          wildcards.push({
            name,
            mode: wm.mode || "randomize",
            index: wm.index || 0,
            label: wm.label || "",
            rolledLabel: wr.label || "",
          });
        }
      }
    }
  }

  const props = node.properties || {};
  return {
    node,
    children,
    labels,
    wildcards,
    title: hasCustomTitle(node) ? node.title : "PromptChain",
    hasChildren: children.length > 0 || labels.length > 0 || wildcards.length > 0,
    // snapshot for Svelte reactivity — tree objects are new each rebuild,
    // but node is the same LGraphNode reference, so $derived(node.properties.*)
    // won't detect changes. Reading from tree.* instead forces re-evaluation.
    mode: props.pcrMode || "switch",
    switchIndex: props.pcrSwitchIndex ?? 1,
    locked: !!props.pcrLocked,
    disabled: !!props.pcrDisabled,
    collapsed: !!props.pcrTreeCollapsed,
  };
}

export function flattenTree(tree, depth = 0, parentNode = null) {
  if (!tree) return [];
  const result = [{ ...tree, depth, parentNode }];
  for (const child of tree.children) {
    result.push(...flattenTree(child, depth + 1, tree.node));
  }
  return result;
}

export function findAllRoots() {
  const seen = new Set();
  const roots = [];
  for (const node of app.graph?._nodes || []) {
    if (!isPromptChain(node)) continue;
    const root = findRoot(node);
    if (!seen.has(root.id)) {
      seen.add(root.id);
      roots.push(root);
    }
  }
  return roots;
}

export function stampInactive(tree) {
  function walk(treeNode, inherited) {
    treeNode._inactive = inherited;
    const mode = treeNode.node.properties?.pcrMode || "switch";
    const switchIndex = treeNode.node.properties?.pcrSwitchIndex ?? 1;

    for (let i = 0; i < treeNode.children.length; i++) {
      const childIndex = i + 1;
      let childInactive = inherited;
      if (!inherited && mode === "switch" && treeNode.children.length > 1) {
        childInactive = switchIndex === 0 || childIndex !== switchIndex;
      }
      walk(treeNode.children[i], childInactive);
    }
  }
  if (tree) walk(tree, false);
}

export function isDescendant(tree, targetId) {
  if (tree.node.id === targetId) return true;
  for (const child of tree.children) {
    if (isDescendant(child, targetId)) return true;
  }
  return false;
}
