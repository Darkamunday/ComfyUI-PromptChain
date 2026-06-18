// Dynamic output slot visibility — auto-shows positive/negative output slots
// when the node is connected downstream to a KSampler via CLIPTextEncode.

import { app } from "../../../scripts/app.js";
import { NODE_TYPE } from "./config.js";
import { getLink } from "./slot-utils.js";

// ── detection helpers ──────────────────────────────────────────────

function isKSamplerNode(node) {
  const cls = node.comfyClass || "";
  if (cls === "KSampler" || cls === "KSamplerAdvanced") return true;
  if (cls.includes("KSampler")) return true;
  // fallback: any node with positive/negative CONDITIONING inputs
  let hasPos = false, hasNeg = false;
  for (const inp of node.inputs || []) {
    if (inp.type === "CONDITIONING") {
      const name = inp.name?.toLowerCase();
      if (name === "positive") hasPos = true;
      if (name === "negative") hasNeg = true;
    }
  }
  return hasPos && hasNeg;
}

function isCLIPTextEncode(node) {
  const cls = node.comfyClass || "";
  return cls === "CLIPTextEncode" || cls.includes("CLIPTextEncode");
}

// ── graph tracing ──────────────────────────────────────────────────

/**
 * Trace from a target node downstream to find which KSampler input the path feeds.
 * Returns "positive", "negative", or null.
 */
function traceLinkToSampler(node, graph, visited) {
  if (!node || visited.has(node.id)) return null;
  visited.add(node.id);

  if (node.comfyClass === NODE_TYPE) return null; // chaining — not a sampler path

  if (isCLIPTextEncode(node)) {
    for (const output of node.outputs || []) {
      if (output.type !== "CONDITIONING" || !output.links?.length) continue;
      for (const lid of output.links) {
        const lnk = getLink(graph, lid);
        if (!lnk) continue;
        const down = graph.getNodeById(lnk.target_id);
        if (!down) continue;
        if (isKSamplerNode(down)) {
          const name = down.inputs?.[lnk.target_slot]?.name?.toLowerCase();
          if (name === "positive") return "positive";
          if (name === "negative") return "negative";
        }
        const r = traceLinkToSampler(down, graph, visited);
        if (r) return r;
      }
    }
    return null;
  }

  // generic node — keep following outputs
  for (const output of node.outputs || []) {
    if (!output.links?.length) continue;
    for (const lid of output.links) {
      const lnk = getLink(graph, lid);
      if (!lnk) continue;
      const down = graph.getNodeById(lnk.target_id);
      if (!down) continue;
      if (isKSamplerNode(down)) {
        const name = down.inputs?.[lnk.target_slot]?.name?.toLowerCase();
        if (name === "positive") return "positive";
        if (name === "negative") return "negative";
      }
      const r = traceLinkToSampler(down, graph, visited);
      if (r) return r;
    }
  }
  return null;
}

/**
 * Categorize all links on out (slot 0).
 * chain = another PromptChain, positive/negative = traced to KSampler,
 * unknown = connected to something we can't trace (show all slots as fallback).
 */
function categorizeOutLinks(node) {
  const graph = app.graph;
  const cats = { chain: [], positive: [], negative: [], unknown: [] };
  if (!graph) return cats;

  const outSlot = node.outputs?.[0];
  if (!outSlot?.links?.length) return cats;

  for (const linkId of [...outSlot.links]) {
    const link = getLink(graph, linkId);
    if (!link) continue;
    const target = graph.getNodeById(link.target_id);
    if (!target) continue;

    if (target.comfyClass === NODE_TYPE) {
      cats.chain.push(linkId);
    } else {
      const cat = traceLinkToSampler(target, graph, new Set());
      if (cat === "positive") cats.positive.push(linkId);
      else if (cat === "negative") cats.negative.push(linkId);
      else cats.unknown.push(linkId);
    }
  }
  return cats;
}

// ── FLUX detection ─────────────────────────────────────────────────

export function isFluxWorkflow() {
  const graph = app.graph;
  if (!graph) return false;
  if (graph._pcrFluxMode !== undefined) return graph._pcrFluxMode;

  let flux = false;
  for (const n of graph._nodes || []) {
    if (n.comfyClass === "DualCLIPLoader") {
      const w = n.widgets?.find(w => w.name === "type");
      if (w?.value?.toLowerCase?.()?.includes("flux")) { flux = true; break; }
    }
    if (n.comfyClass === "UNETLoader") {
      const w = n.widgets?.find(w => w.name === "unet_name");
      if (w?.value?.toLowerCase?.()?.includes("flux")) { flux = true; break; }
    }
  }
  graph._pcrFluxMode = flux;
  return flux;
}

export function invalidateFluxCache() {
  if (app.graph) delete app.graph._pcrFluxMode;
}

// ── vue reactivity ─────────────────────────────────────────────────

function forceOutputRefresh(node) {
  requestAnimationFrame(() => {
    // guard: node may have been deleted before this rAF fires
    if (!app.graph?.getNodeById(node.id)) return;
    const savedSize = node.size ? [...node.size] : null;
    app.graph.onNodeRemoved?.(node);
    app.graph.onNodeAdded?.(node);
    // restore size — Vue re-render from onNodeAdded can reset to minimum
    if (savedSize) node.setSize?.(savedSize);
    app.graph.setDirtyCanvas?.(true, true);
  });
}

// ── slot management ────────────────────────────────────────────────

const _mutating = new WeakSet();

// Slot meaning is positional: 0=out, 1=positive, 2=negative, 3=regions.
// THE INVARIANT: the backend returns outputs by FIXED index, so an output may
// only be removed when nothing at or after it is linked — LiteGraph's
// removeOutput slides later outputs down (regions 3→1), the save persists the
// shifted origin_slot, and execution then feeds the POSITIVE string into a
// regions input (flat text into the couple/conditioning — a real shipped bug).
function linkedAtOrAfter(node, idx) {
  return (node.outputs || []).some((o, i) => i >= idx && o.links?.length > 0);
}

function ensureSlotCount(node, count) {
  const cur = node.outputs?.length || 0;
  if (count > cur) {
    if (cur < 2 && count >= 2) node.addOutput("positive", "STRING");
    if ((node.outputs?.length || 0) < 3 && count >= 3) node.addOutput("negative", "STRING");
  } else if (count < cur) {
    if (cur >= 3 && count < 3 && !linkedAtOrAfter(node, 2)) node.removeOutput(2);
    if ((node.outputs?.length || 0) >= 2 && count < 2 && !linkedAtOrAfter(node, 1)) node.removeOutput(1);
  }
}

// Two-pass disconnect-then-reconnect: LiteGraph mutates the links array
// during disconnect, so resolving every target before the first disconnect
// is the only way to keep iteration stable.
function migrateLinks(node, linkIds, toSlot, graph) {
  if (!linkIds.length) return;

  const targets = [];
  for (const lid of linkIds) {
    const link = getLink(graph, lid);
    if (!link) continue;
    const targetNode = graph.getNodeById(link.target_id);
    if (!targetNode) continue;
    targets.push({ node: targetNode, slot: link.target_slot });
  }

  for (const t of targets) node.disconnectOutput(0, t.node);
  for (const t of targets) node.connect(toSlot, t.node, t.slot);
}

// ── public API ─────────────────────────────────────────────────────

export function updateDynamicOutputs(node) {
  if (_mutating.has(node)) return;
  if (node._pcrInitializing) return;

  const autoSplit = app.ui?.settings?.getSettingValue?.("PromptChain.AutoSplitOutputs") ?? true;

  _mutating.add(node);
  try {
    if (!autoSplit) {
      ensureSlotCount(node, 3);
    } else {
      const cats = categorizeOutLinks(node);
      const hasKSampler = cats.positive.length > 0 || cats.negative.length > 0;
      const hasUnknown = cats.unknown.length > 0;

      if (hasUnknown) {
        // unknown target — show all 3, let the user wire manually
        ensureSlotCount(node, 3);
      } else if (hasKSampler) {
        const flux = isFluxWorkflow();
        ensureSlotCount(node, flux ? 2 : 3);
        const graph = app.graph;
        if (graph) {
          migrateLinks(node, cats.positive, 1, graph);
          if (!flux) migrateLinks(node, cats.negative, 2, graph);
        }
      } else {
        // no KSampler paths — collapse the extra outputs only when NOTHING
        // beyond `out` is connected (regions included: a regional graph wires
        // regions while pos/neg dangle, and removing pos/neg would slide the
        // linked regions output off its backend index). Collapse goes all the
        // way to [out] so the remaining array stays positionally canonical.
        if (!linkedAtOrAfter(node, 1)) {
          while ((node.outputs?.length || 0) > 1) node.removeOutput(node.outputs.length - 1);
        }
      }
    }

    forceOutputRefresh(node);
  } finally {
    _mutating.delete(node);
  }
}

/**
 * Trace upstream through CONDITIONING links from a node to find a text encoder
 * (any node with a free STRING "text" or "prompt" input).
 */
function findTextEncoderUpstream(node, graph, visited) {
  if (!node || visited.has(node.id)) return null;
  visited.add(node.id);

  const textInput = node.inputs?.find(i =>
    i.type === "STRING" && (i.name === "text" || i.name === "prompt") && i.link == null
  );
  if (textInput) return node;

  for (const inp of node.inputs || []) {
    if (inp.type !== "CONDITIONING" || inp.link == null) continue;
    const link = getLink(graph, inp.link);
    if (!link) continue;
    const source = graph.getNodeById(link.origin_id);
    const result = findTextEncoderUpstream(source, graph, visited);
    if (result) return result;
  }
  return null;
}

/**
 * Auto-connect a freshly dropped PromptChain to free text encoder nodes
 * feeding a KSampler. Conditions (all must be true):
 *   1. Text encoder(s) upstream of a KSampler's positive (and optionally negative)
 *   2. Exactly 1 KSampler in the graph has text encoders wired to it
 *   3. Those text encoder inputs have no existing connections
 *   4. No other PromptChain nodes exist in the graph
 */
export function autoConnectToSampler(node) {
  const autoSplit = app.ui?.settings?.getSettingValue?.("PromptChain.AutoSplitOutputs") ?? true;
  if (!autoSplit) return;

  const graph = app.graph;
  if (!graph) return;

  // condition 4: no other PromptChain nodes
  for (const n of graph._nodes || []) {
    if (n.comfyClass === NODE_TYPE && n.id !== node.id) return;
  }

  // scan for KSampler nodes with text encoders upstream of their conditioning inputs
  const candidates = [];

  for (const n of graph._nodes || []) {
    if (!isKSamplerNode(n)) continue;

    let posEncoder = null, negEncoder = null;

    for (const inp of n.inputs || []) {
      if (inp.type !== "CONDITIONING" || inp.link == null) continue;
      const link = getLink(graph, inp.link);
      if (!link) continue;
      const source = graph.getNodeById(link.origin_id);
      const encoder = findTextEncoderUpstream(source, graph, new Set());
      if (!encoder) continue;

      const name = inp.name?.toLowerCase();
      if (name === "positive") posEncoder = encoder;
      else if (name === "negative") negEncoder = encoder;
    }

    if (posEncoder) candidates.push({ posEncoder, negEncoder });
  }

  // condition 2: exactly 1 qualifying KSampler
  if (candidates.length !== 1) return;

  const { posEncoder, negEncoder } = candidates[0];

  // condition 3: text encoder inputs must be free (already checked by findTextEncoderUpstream)
  const posTextInput = posEncoder.inputs?.find(i =>
    i.type === "STRING" && (i.name === "text" || i.name === "prompt")
  );
  if (!posTextInput || posTextInput.link != null) return;

  let negTextInput = null;
  if (negEncoder) {
    negTextInput = negEncoder.inputs?.find(i =>
      i.type === "STRING" && (i.name === "text" || i.name === "prompt")
    );
    if (!negTextInput || negTextInput.link != null) return;
  }

  // read existing widget text before connecting (connection converts widget to input)
  const posText = posEncoder.widgets?.find(w => w.name === posTextInput.name)?.value || "";
  let negText = "";
  if (negEncoder && negTextInput) {
    negText = negEncoder.widgets?.find(w => w.name === negTextInput.name)?.value || "";
  }

  // all conditions met — expand slots and connect
  _mutating.add(node);
  try {
    const flux = isFluxWorkflow();
    ensureSlotCount(node, (negEncoder && !flux) ? 3 : 2);

    const posTextIdx = posEncoder.inputs.indexOf(posTextInput);
    node.connect(1, posEncoder, posTextIdx);

    if (negEncoder && negTextInput && !flux) {
      const negTextIdx = negEncoder.inputs.indexOf(negTextInput);
      node.connect(2, negEncoder, negTextIdx);
    }

    forceOutputRefresh(node);
  } finally {
    _mutating.delete(node);
  }

  // return captured text so caller can seed the editor
  if (posText || negText) {
    return { posText: posText.trim(), negText: negText.trim() };
  }
  return null;
}

/**
 * One-time slot setup on node creation / workflow load.
 * Removes pos/neg if unconnected; preserves them if workflow had connections.
 */
export function initializeOutputSlots(node, { skipRefresh = false } = {}) {
  const autoSplit = app.ui?.settings?.getSettingValue?.("PromptChain.AutoSplitOutputs") ?? true;
  if (!autoSplit) return; // all 3 visible from Python

  // Preserve ANY connection past `out` — collapsing with a linked regions
  // output slid it from slot 3 to slot 1, and the backend (fixed indices)
  // then fed the positive string into the regions consumer.
  if (linkedAtOrAfter(node, 1)) return;

  _mutating.add(node);
  try {
    while ((node.outputs?.length || 0) > 1) node.removeOutput(node.outputs.length - 1);
    // skip during graph load — forceOutputRefresh fires a rAF that would
    // mutate the graph after ComfyUI's change-tracker snapshot, causing
    // false "unsaved changes" detection
    if (!skipRefresh) forceOutputRefresh(node);
  } finally {
    _mutating.delete(node);
  }
}

// Repair slot-drifted regions links at load. The collapse bug above shipped:
// saves exist where a link into a consumer input NAMED "regions" originates
// from the positive/out output (origin_slot persisted after the slide). The
// target input name states the wiring intent unambiguously — remap the link
// to the actual regions output. Outputs are rebuilt to canonical order first
// when drifted, so the link lands on the regions BACKEND index.
const CANONICAL_OUTPUTS = ["out", "positive", "negative", "regions"];
export function healRegionsLinks(node) {
  const graph = app.graph;
  if (!graph || !node.outputs?.length) return;
  const drifted = [];
  node.outputs.forEach((o, i) => {
    if (o.name === "regions") return;
    for (const lid of [...(o.links || [])]) {
      const link = getLink(graph, lid);
      const target = link && graph.getNodeById(link.target_id);
      const input = target?.inputs?.[link?.target_slot];
      if (input?.name === "regions") drifted.push({ fromSlot: i, target, toSlot: link.target_slot });
    }
  });
  const regionsAt = node.outputs.findIndex(o => o.name === "regions");
  const canonical = node.outputs.every((o, i) => o.name === CANONICAL_OUTPUTS[i]);
  if (!drifted.length && (canonical || regionsAt === -1 || !node.outputs[regionsAt].links?.length)) return;

  _mutating.add(node);
  try {
    // capture every output's targets BY NAME, rebuild canonical, reconnect
    const byName = {};
    for (const o of node.outputs) {
      byName[o.name] = (o.links || []).map(lid => getLink(graph, lid)).filter(Boolean)
        .map(l => ({ target: graph.getNodeById(l.target_id), toSlot: l.target_slot }))
        .filter(t => t.target);
    }
    // drifted links belong to regions regardless of which output held them
    for (const d of drifted) {
      byName[node.outputs[d.fromSlot].name] =
        (byName[node.outputs[d.fromSlot].name] || []).filter(t => !(t.target === d.target && t.toSlot === d.toSlot));
      (byName.regions = byName.regions || []).push({ target: d.target, toSlot: d.toSlot });
    }
    for (let i = node.outputs.length - 1; i >= 0; i--) node.removeOutput(i);
    for (const name of CANONICAL_OUTPUTS) node.addOutput(name, "STRING");
    CANONICAL_OUTPUTS.forEach((name, idx) => {
      for (const t of byName[name] || []) node.connect(idx, t.target, t.toSlot);
    });
    if (drifted.length) console.warn(`[PromptChain] healed ${drifted.length} drifted regions link(s) on node ${node.id}`);
  } finally {
    _mutating.delete(node);
  }
}
