function readNodePrompt(node) {
  var _a, _b, _c;
  const view = node == null ? void 0 : node._pcrEditor;
  if ((_a = view == null ? void 0 : view.state) == null ? void 0 : _a.doc) return view.state.doc.toString();
  const widget = (_c = (_b = node == null ? void 0 : node.widgets) == null ? void 0 : _b.find) == null ? void 0 : _c.call(_b, (w) => w.name === "prompt");
  return (widget == null ? void 0 : widget.value) ?? "";
}
function isPromptChainNode(n) {
  var _a;
  return (n == null ? void 0 : n.comfyClass) === "PromptChain_PromptChain" || ((_a = n == null ? void 0 : n.constructor) == null ? void 0 : _a.comfyClass) === "PromptChain_PromptChain" || (n == null ? void 0 : n.type) === "PromptChain_PromptChain";
}
function isStandaloneMainPromptChain(n) {
  var _a, _b, _c;
  if (!n) return false;
  const inputs = (n.inputs || []).filter(
    (slot) => {
      var _a2, _b2;
      return (((_a2 = slot.name) == null ? void 0 : _a2.startsWith("in_")) || ((_b2 = slot.name) == null ? void 0 : _b2.startsWith("inputs.in_"))) && slot.link != null;
    }
  );
  if (inputs.length > 0) return false;
  const graph = n.graph || typeof window !== "undefined" && ((_a = window.app) == null ? void 0 : _a.graph);
  if (!graph) return true;
  for (const out of n.outputs || []) {
    if (out.name !== "out") continue;
    for (const lid of out.links || []) {
      const link = (_b = graph.links) == null ? void 0 : _b[lid];
      if (!link) continue;
      const target = (_c = graph.getNodeById) == null ? void 0 : _c.call(graph, link.target_id);
      if (target && isPromptChainNode(target)) return false;
    }
  }
  return true;
}
function extractNGrams(text) {
  if (!text) return [];
  const words = text.replace(/\(\s*([^():]+?)\s*:\s*[\d.]+\s*\)/g, "$1").replace(/'s\b/gi, "").replace(/[^\w\s()'\\-]/g, " ").split(/\s+/).filter(Boolean);
  const out = /* @__PURE__ */ new Set();
  for (let n = 1; n <= 4; n++) {
    for (let i = 0; i + n <= words.length; i++) {
      const phrase = words.slice(i, i + n).join(" ").trim();
      if (!phrase) continue;
      if (n === 1 && phrase.length < 3) continue;
      out.add(phrase);
    }
  }
  for (const variant of synthesizeParenVariants(text)) {
    out.add(variant);
  }
  return Array.from(out);
}
function synthesizeParenVariants(text) {
  if (!text) return [];
  const variants = [];
  const prepRe = /\s+(?:from|in|of|for|as)\s+/gi;
  let m;
  while ((m = prepRe.exec(text)) !== null) {
    const leftText = text.slice(0, m.index).trim();
    const rightText = text.slice(m.index + m[0].length).trim();
    if (!leftText || !rightText) continue;
    const leftWords = leftText.replace(/[^\w\s'\\-]/g, " ").split(/\s+/).filter(Boolean);
    const rightWords = rightText.replace(/[^\w\s'\\-]/g, " ").split(/\s+/).filter(Boolean);
    for (let nl = 1; nl <= Math.min(3, leftWords.length); nl++) {
      const name = leftWords.slice(-nl).join(" ");
      for (let rl = 1; rl <= Math.min(3, rightWords.length); rl++) {
        const series = rightWords.slice(0, rl).join(" ");
        variants.push(`${name} (${series})`);
      }
    }
  }
  return variants;
}
async function matchCharactersInDb(tokens, userText, nodePromptText) {
  if (!Array.isArray(tokens) || tokens.length === 0) return [];
  try {
    const r = await fetch("/promptchain/tag-builder/match-characters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tokens,
        user_text: userText || "",
        node_prompt: nodePromptText || ""
      })
    });
    if (!r.ok) {
      console.warn("[PromptChain ai-patch] match-characters failed:", r.status);
      return [];
    }
    const { matched } = await r.json();
    return Array.isArray(matched) ? matched : [];
  } catch (err) {
    console.warn("[PromptChain ai-patch] match-characters error:", err);
    return [];
  }
}
function isStructuralLine(line) {
  const t = line.trim();
  if (!t) return true;
  if (t.startsWith("//")) return true;
  if (/^[A-Z][a-zA-Z ]+ Prompt:\s*$/.test(t)) return true;
  return false;
}
function isInNegativeBlock(lines, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const t = lines[i].trim();
    if (/^[A-Z][a-zA-Z ]+ Prompt:\s*$/.test(t)) {
      return /^Negative Prompt:/i.test(t);
    }
  }
  return false;
}
function applyPromptText(view, outputText, node, promptState) {
  if (!view || !outputText) return false;
  const source = view.state.doc.toString();
  if (outputText !== source) {
    view.dispatch({ changes: { from: 0, to: source.length, insert: outputText } });
  }
  if (node && promptState !== void 0) {
    if (!node.properties) node.properties = {};
    node.properties.pcrPromptState = promptState;
  }
  return true;
}
function cryptoId() {
  var _a;
  if (typeof window !== "undefined" && ((_a = window.crypto) == null ? void 0 : _a.randomUUID)) {
    return window.crypto.randomUUID().replace(/-/g, "");
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}
export {
  applyPromptText as a,
  isStructuralLine as b,
  cryptoId as c,
  isInNegativeBlock as d,
  extractNGrams as e,
  isStandaloneMainPromptChain as i,
  matchCharactersInDb as m,
  readNodePrompt as r
};
//# sourceMappingURL=ai-patch-helpers-Bayqv0oF.js.map
