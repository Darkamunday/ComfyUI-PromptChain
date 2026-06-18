// Iterate hierarchy detection, cascade advancement, and revert system.
// Coordinates multi-node iterate chains so subordinates advance in lockstep.
//
// Key invariant: innermost nodes exhaust fully before parents advance.
// This is nested-for-loop order, NOT breadth-first/round-robin.

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { NODE_TYPE } from "./config.js";
import { getLink } from "./slot-utils.js";
import { updateParentLabels } from "./order-chain.js";

/** Extract the numeric suffix from an autogrow input name ("in_0" or "inputs.in_0" → 0). */
function parseInNumber(name) {
  if (!name) return null;
  // V3 autogrow: "inputs.in_0", legacy: "in_0"
  const match = name.match(/(?:^|inputs\.)in_(\d+)$/);
  return match ? parseInt(match[1], 10) : null;
}

/** Check if an input slot is an autogrow in_ slot. */
function isAutogrowInput(name) {
  return parseInNumber(name) !== null;
}

// ── hierarchy detection ────────────────────────────────────────────

/**
 * Scan all PromptChain nodes, detect iterate hierarchies, and register
 * subordinate nodes with the server. Runs before each queue.
 */
export function detectIterateHierarchies() {
  const graph = app.graph;
  if (!graph) return;

  const nodes = (graph._nodes || []).filter(n => n.comfyClass === NODE_TYPE);

  // step 1: clear all flags
  for (const node of nodes) {
    if (!node.properties) node.properties = {};
    node.properties.pcrSubordinate = false;
    node.properties.pcrSubordinateTo = null;
    node.properties.pcrSiblingMaster = false;
    node.properties.pcrInnermostSubordinate = false;
    node.properties.pcrChainSubordinate = false;
    node.properties.pcrChainSubordinateSlot = null;
    node.properties.pcrHasChainSubordinates = false;
  }

  // step 2: build downstream map (which PromptChain nodes does each feed?)
  const downstreamMap = new Map();
  for (const node of nodes) {
    for (const output of node.outputs || []) {
      if (!output.links?.length) continue;
      for (const linkId of output.links) {
        const link = getLink(graph, linkId);
        if (!link) continue;
        const target = graph.getNodeById(link.target_id);
        if (target?.comfyClass === NODE_TYPE) {
          if (!downstreamMap.has(node.id)) downstreamMap.set(node.id, []);
          downstreamMap.get(node.id).push({
            targetId: target.id,
            targetSlot: link.target_slot,
          });
        }
      }
    }
  }

  // step 3: find iterate-mode nodes
  const iterateNodes = nodes.filter(n => n.properties?.pcrMode === "iterate");

  // step 4: BFS helper — find the iterate node a given node eventually reaches
  // Traces through non-iterate intermediaries (combine/switch nodes).
  // Returns { nodeId, slotIndex } or null.
  function findDownstreamIterateNode(startNodeId, excludeNodeId) {
    const visited = new Set();
    const queue = [startNodeId];

    while (queue.length > 0) {
      const currentId = queue.shift();
      if (visited.has(currentId)) continue;
      visited.add(currentId);

      for (const { targetId, targetSlot } of downstreamMap.get(currentId) || []) {
        if (targetId === excludeNodeId) continue;
        const target = graph.getNodeById(targetId);
        if (!target) continue;

        const targetMode = target.properties?.pcrMode;

        if (targetMode === "iterate") {
          // Found an iterate node. Compute the iterate position (not raw slot index).
          // Python sorts connected in_X inputs by X and uses position in that sorted list.
          const inputName = target.inputs?.[targetSlot]?.name;
          const inNumber = parseInNumber(inputName);

          if (inNumber !== null) {
            const connectedInNumbers = (target.inputs || [])
              .filter(inp => isAutogrowInput(inp.name) && inp.link != null)
              .map(inp => parseInNumber(inp.name))
              .filter(n => n !== null)
              .sort((a, b) => a - b);
            const iteratePosition = connectedInNumbers.indexOf(inNumber);
            return { nodeId: targetId, slotIndex: iteratePosition !== -1 ? iteratePosition : targetSlot };
          }
          return { nodeId: targetId, slotIndex: targetSlot };
        }

        // Not iterate — continue tracing through it
        if (!visited.has(targetId)) queue.push(targetId);
      }
    }
    return null;
  }

  // step 5: BFS helper — trace to ALL downstream nodes (for sibling detection)
  // Returns [{ nodeId, slotIndex, depth }] with inherited slot and hop count.
  function traceToDownstreamNodes(startNodeId) {
    const visited = new Set();
    const queue = [{ id: startNodeId, inheritedSlot: null, depth: 0 }];
    const results = [];

    while (queue.length > 0) {
      const { id: currentId, inheritedSlot, depth } = queue.shift();
      if (visited.has(currentId)) continue;
      visited.add(currentId);

      for (const { targetId, targetSlot } of downstreamMap.get(currentId) || []) {
        const effectiveSlot = inheritedSlot !== null ? inheritedSlot : targetSlot;
        const target = graph.getNodeById(targetId);
        if (!target) continue;

        results.push({ nodeId: targetId, slotIndex: effectiveSlot, depth: depth + 1 });

        // Continue tracing through non-iterate nodes
        const targetMode = target.properties?.pcrMode;
        if (targetMode !== "iterate" && !visited.has(targetId)) {
          queue.push({ id: targetId, inheritedSlot: effectiveSlot, depth: depth + 1 });
        }
      }
    }
    return results;
  }

  // step 6: detect chain subordinates (iterate → [intermediaries →] iterate)
  const chainSubordinates = new Map(); // iterateNodeId → downstreamIterateNodeId
  for (const node of iterateNodes) {
    const downstream = findDownstreamIterateNode(node.id, node.id);
    if (downstream) {
      node.properties.pcrSubordinate = true;
      node.properties.pcrSubordinateTo = downstream.nodeId;
      node.properties.pcrChainSubordinate = true;
      node.properties.pcrChainSubordinateSlot = downstream.slotIndex;
      chainSubordinates.set(node.id, downstream.nodeId);
    }
  }

  // step 7: mark nodes that have chain subordinates pointing to them
  for (const [chainSubId, targetId] of chainSubordinates) {
    const target = graph.getNodeById(targetId);
    if (target) {
      if (!target.properties) target.properties = {};
      target.properties.pcrHasChainSubordinates = true;
    }
  }

  // step 8: detect sibling iterates (multiple iterates → same downstream node)
  // Iterate nodes that share an output path should coordinate as nested for loops.
  // Inner nodes (deeper in graph) exhaust before outer nodes advance.
  const siblingMap = new Map(); // downstreamNodeId → [{ iterateNodeId, slotIndex, depth }]
  for (const iterNode of iterateNodes) {
    for (const { nodeId, slotIndex, depth } of traceToDownstreamNodes(iterNode.id)) {
      if (!siblingMap.has(nodeId)) siblingMap.set(nodeId, []);
      const existing = siblingMap.get(nodeId);
      if (!existing.find(e => e.iterateNodeId === iterNode.id)) {
        existing.push({ iterateNodeId: iterNode.id, slotIndex, depth });
      }
    }
  }

  const siblingMasterIds = [];
  for (const [downstreamId, iterateInputs] of siblingMap) {
    if (iterateInputs.length < 2) continue;

    const downstreamNode = graph.getNodeById(downstreamId);
    const downstreamMode = downstreamNode?.properties?.pcrMode;

    if (downstreamMode === "iterate") {
      // Iterate-mode downstream: slot-based coordination (legacy behavior).
      // Skip if iterate inputs feed DIFFERENT slots — those are alternatives, not siblings.
      const parentSlots = iterateInputs.map(input => {
        const n = graph.getNodeById(input.iterateNodeId);
        return n?.properties?.pcrChainSubordinateSlot;
      });
      if (!parentSlots.every(s => s === parentSlots[0])) continue;

      // Sort by slot index: lower slot = outer (master), higher slot = inner
      iterateInputs.sort((a, b) => a.slotIndex - b.slotIndex);
    } else {
      // Non-iterate downstream (combine/switch): depth-based coordination.
      // All iterate nodes sharing this output path become nested for loops.
      // Deeper nodes (more hops) = inner (exhaust first).
      // Shallower nodes = outer (advance when inner wraps).
      iterateInputs.sort((a, b) => a.depth - b.depth);
    }

    const masterIterateId = iterateInputs[0].iterateNodeId;
    const masterNode = graph.getNodeById(masterIterateId);

    // Mark the master (outer — does not auto-advance, waits for inner to wrap)
    if (masterNode) {
      if (!masterNode.properties) masterNode.properties = {};
      masterNode.properties.pcrSiblingMaster = true;
      if (!siblingMasterIds.includes(masterIterateId)) {
        siblingMasterIds.push(masterIterateId);
      }
    }

    // Build sibling chain: each node points to the one before it in sorted order
    for (let i = 1; i < iterateInputs.length; i++) {
      const subNode = graph.getNodeById(iterateInputs[i].iterateNodeId);
      if (!subNode) continue;
      if (!subNode.properties) subNode.properties = {};

      const immediateParentId = iterateInputs[i - 1].iterateNodeId;

      // Sibling relationship takes precedence over chain subordinate
      subNode.properties.pcrSubordinate = true;
      subNode.properties.pcrSubordinateTo = immediateParentId;
      subNode.properties.pcrChainSubordinate = false; // clear chain flag

      // Only mark as innermost if: last in group AND master is not itself subordinate
      const isLast = i === iterateInputs.length - 1;
      const masterIsSubordinate = masterNode?.properties?.pcrSubordinate === true;
      subNode.properties.pcrInnermostSubordinate = isLast && !masterIsSubordinate;

    }
  }

  // step 9: collect no-auto-advance IDs
  const noAutoAdvanceIds = [];
  for (const node of iterateNodes) {
    if (node.properties.pcrSubordinate ||
        node.properties.pcrSiblingMaster ||
        node.properties.pcrHasChainSubordinates) {
      noAutoAdvanceIds.push(String(node.id));
    }
  }
  // also include chain masters that aren't already sibling masters
  for (const node of nodes) {
    if (node.properties?.pcrHasChainSubordinates &&
        !siblingMasterIds.includes(node.id) &&
        !noAutoAdvanceIds.includes(String(node.id))) {
      noAutoAdvanceIds.push(String(node.id));
    }
  }

  // step 10: POST subordinates to server (awaited by caller)
  return fetch("/promptchain/iterate/set-subordinates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_ids: noAutoAdvanceIds }),
  }).catch(() => {});
}

// ── cascade advancement ────────────────────────────────────────────

/**
 * Find the deepest chain subordinate of a node.
 * If the node is in iterate mode, only considers subordinates feeding
 * the currently-active input slot (handles alternatives pattern).
 */
function findInnermostChainSubordinate(node) {
  const graph = app.graph;
  if (!graph) return null;

  let subs = (graph._nodes || []).filter(n =>
    n.comfyClass === NODE_TYPE &&
    n.properties?.pcrChainSubordinate &&
    n.properties?.pcrSubordinateTo === node.id
  );

  // If this node is in iterate mode, filter to subordinates feeding the active slot.
  // This handles alternatives: ryu outfits on slot 0, bison outfits on slot 1 —
  // only advance the one that just ran.
  const nodeMode = node.properties?.pcrMode;
  if (nodeMode === "iterate") {
    // Use pcrIterateCurrent (what just executed), NOT pcrIterateIndex (the NEXT index)
    const currentIndex = node.properties?.pcrIterateCurrent ?? 0;
    subs = subs.filter(n => n.properties?.pcrChainSubordinateSlot === currentIndex);
  }

  if (subs.length === 0) return null;

  // Find the one with no other subs pointing to it (the deepest leaf)
  for (const sub of subs) {
    const hasSubsPointingToIt = subs.some(other =>
      other.id !== sub.id && other.properties?.pcrSubordinateTo === sub.id
    );
    if (!hasSubsPointingToIt) return sub;
  }
  // fallback
  return subs[0];
}

/**
 * Advance a node's iterate state and cascade upward on wrap.
 * fromSubordinate: true when cascading UP from a child — skips redirect checks.
 */
export async function advanceAndCascade(node, depth = 0, fromSubordinate = false) {
  if (depth > 10) return; // safety limit

  // Before advancing, check if this node has subordinates.
  // If so, redirect to the innermost — it must exhaust first.
  // Skip this check when cascading up (fromSubordinate) to prevent loops.
  if (!fromSubordinate) {
    // check chain subordinates first
    const chainSub = findInnermostChainSubordinate(node);
    if (chainSub && chainSub.id !== node.id) {
      return advanceAndCascade(chainSub, depth, false);
    }
    // check sibling subordinates — master redirects to its innermost
    if (node.properties?.pcrSiblingMaster) {
      const graph = app.graph;
      const siblingSub = graph?._nodes?.find(n =>
        n.comfyClass === NODE_TYPE &&
        n.properties?.pcrSubordinateTo === node.id &&
        n.properties?.pcrInnermostSubordinate === true
      );
      if (siblingSub) {
        return advanceAndCascade(siblingSub, depth, false);
      }
    }
  }

  const hash = node.properties?.pcrIterateContentHash;
  if (!hash) return;

  try {
    const resp = await fetch("/promptchain/iterate/advance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content_hash: hash }),
    });
    if (!resp.ok) return;
    const data = await resp.json();

    // record for revert
    _recordAdvancement(hash, data.prev_index, data.prev_cycle);

    // update node properties + UI
    node.properties.pcrIterateIndex = data.new_index;
    node.properties.pcrIterateCycle = data.new_cycle;
    node._pcrMenubar?.updateModeDisplay?.();
    updateParentLabels(node);
    app.graph?.setDirtyCanvas?.(true);

    // cascade upward on wrap
    if (data.wrapped) {
      const parentId = node.properties.pcrSubordinateTo;
      if (parentId) {
        const parent = app.graph?.getNodeById(parentId);
        if (parent) {
          await advanceAndCascade(parent, depth + 1, true);
        }
      }
    }
  } catch (e) {
    console.error("[PromptChain] advance failed:", e);
  }
}

// ── revert system ──────────────────────────────────────────────────

let _pendingReverts = new Map();  // promptId → [{contentHash, prevIndex, prevCycle}]
let _currentPromptId = null;
const _promptHasSave = new Set(); // promptIds that had SaveImage output (type=output, not preview)

function _recordAdvancement(contentHash, prevIndex, prevCycle) {
  if (!_currentPromptId) return;
  if (!_pendingReverts.has(_currentPromptId)) {
    _pendingReverts.set(_currentPromptId, []);
  }
  _pendingReverts.get(_currentPromptId).push({ contentHash, prevIndex, prevCycle });
}

async function _revertAdvancements(promptId) {
  const reverts = _pendingReverts.get(promptId);
  if (!reverts?.length) return;

  for (const { contentHash, prevIndex, prevCycle } of reverts) {
    try {
      await fetch("/promptchain/iterate/set-state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content_hash: contentHash, index: prevIndex, cycle: prevCycle }),
      });
    } catch (e) {
      console.error("[PromptChain] revert failed:", e);
    }
  }
  _pendingReverts.delete(promptId);
}

// ── event listeners ────────────────────────────────────────────────

/**
 * Wire up all execution lifecycle listeners. Call once from setup().
 */
export function setupIterateListeners() {
  // detect hierarchies before each queue — override queuePrompt so the
  // subordinate POST completes before the prompt is sent to the server
  const origQueuePrompt = app.queuePrompt?.bind(app);
  if (origQueuePrompt) {
    app.queuePrompt = async function (...args) {
      await detectIterateHierarchies();
      return origQueuePrompt(...args);
    };
  }

  // track current prompt for revert system
  api.addEventListener("execution_start", ({ detail }) => {
    _currentPromptId = detail.prompt_id;
  });

  // detect SaveImage output for revert decision — only count permanent saves (type=output),
  // not previews (type=temp). Preview-only runs should revert iterate advancements.
  api.addEventListener("executed", ({ detail }) => {
    if (detail.output?.images?.some(img => img.type === "output")) {
      _promptHasSave.add(detail.prompt_id);
    }
  });

  // revert on error
  api.addEventListener("execution_error", ({ detail }) => {
    _revertAdvancements(detail.prompt_id);
    _promptHasSave.delete(detail.prompt_id);
  });

  // revert on interrupt
  api.addEventListener("execution_interrupted", ({ detail }) => {
    _revertAdvancements(detail.prompt_id);
    _promptHasSave.delete(detail.prompt_id);
  });

  // keep or revert on success (depends on SaveImage)
  api.addEventListener("execution_success", ({ detail }) => {
    if (!_promptHasSave.has(detail.prompt_id)) {
      // no save → preview only → revert
      _revertAdvancements(detail.prompt_id);
    } else {
      // save occurred → keep advancements
      _pendingReverts.delete(detail.prompt_id);
    }
    _promptHasSave.delete(detail.prompt_id);
  });
}
