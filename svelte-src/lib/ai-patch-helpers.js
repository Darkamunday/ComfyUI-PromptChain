// Patch helpers shared by AIAssistant.svelte's legacy single-shot path and
// the new chat agent flow. Extracted verbatim — every body kept identical
// to the originals so the patch pipeline sees byte-equivalent inputs.

// Pull the live editor text — `node._pcrEditor` is rebound to the
// fullscreen group editor when applicable, so this works in both modes.
// Falls back to the prompt widget value if the CM view isn't ready yet.
export function readNodePrompt(node) {
  const view = node?._pcrEditor;
  if (view?.state?.doc) return view.state.doc.toString();
  const widget = node?.widgets?.find?.(w => w.name === "prompt");
  return widget?.value ?? "";
}

// Standalone-main = top of its chain (its `out` output feeds nothing
// OR feeds non-PromptChain consumers like a CLIPTextEncode) AND has
// no PromptChain children feeding into it.
export function isPromptChainNode(n) {
  return n?.comfyClass === "PromptChain_PromptChain"
      || n?.constructor?.comfyClass === "PromptChain_PromptChain"
      || n?.type === "PromptChain_PromptChain";
}

export function isStandaloneMainPromptChain(n) {
  if (!n) return false;
  const inputs = (n.inputs || []).filter(slot =>
    (slot.name?.startsWith("in_") || slot.name?.startsWith("inputs.in_"))
    && slot.link != null
  );
  if (inputs.length > 0) return false;
  const graph = n.graph || (typeof window !== "undefined" && window.app?.graph);
  if (!graph) return true;
  for (const out of n.outputs || []) {
    if (out.name !== "out") continue;
    for (const lid of out.links || []) {
      const link = graph.links?.[lid];
      if (!link) continue;
      const target = graph.getNodeById?.(link.target_id);
      if (target && isPromptChainNode(target)) return false;
    }
  }
  return true;
}

// ---- Tag Builder preflight ----
// Extract n-grams from the user's request and call match-characters
// (exact-match against TB chars table) so /ai/patch sees pre-loaded
// bios for every character the user mentioned.

export function extractNGrams(text) {
  if (!text) return [];
  // Strip A1111 weight syntax first: `(cammy white:1.1)` → `cammy white`.
  // Then strip possessive 's so "cammy white's outfit" tokenizes as
  // [cammy, white, outfit] — surface bare 2-grams for downstream DB lookup.
  const words = text
    .replace(/\(\s*([^():]+?)\s*:\s*[\d.]+\s*\)/g, "$1")
    .replace(/'s\b/gi, "")
    .replace(/[^\w\s()'\\-]/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  const out = new Set();
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

export function synthesizeParenVariants(text) {
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

export async function matchCharactersInDb(tokens, userText, nodePromptText) {
  if (!Array.isArray(tokens) || tokens.length === 0) return [];
  try {
    const r = await fetch("/promptchain/tag-builder/match-characters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tokens,
        user_text: userText || "",
        node_prompt: nodePromptText || "",
      }),
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

// Canonicalize for token matching: trim, drop backslash escapes used
// by SD to literalize parens, swap underscores to spaces, collapse
// internal whitespace.
export function canonicalize(s) {
  return (s || "")
    .trim()
    .replace(/\\\(/g, "(")
    .replace(/\\\)/g, ")")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ");
}

export const WEIGHTED_TOKEN_RE = /^\(\s*(.+?)\s*:\s*[\d.]+\s*\)$/;

// Match a source token against a target token. Direct canonical equality,
// OR — if the source is a weighted form `(inner:weight)` — match by inner.
export function tokenMatches(srcToken, target) {
  const a = canonicalize(srcToken);
  const b = canonicalize(target);
  if (a === b) return true;
  const m = a.match(WEIGHTED_TOKEN_RE);
  if (m && canonicalize(m[1]) === b) return true;
  return false;
}

// Tokenize a line on commas, preserving paren/bracket weighted forms.
export function tokenizeLine(line) {
  const tokens = [];
  let depth = 0;
  let buf = "";
  for (const ch of line) {
    if (ch === "(" || ch === "[") depth++;
    else if (ch === ")" || ch === "]") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      if (buf.trim()) tokens.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (buf.trim()) tokens.push(buf.trim());
  return tokens;
}

// Section-header heuristic: section comments and `Negative Prompt:` are
// NOT tag-content lines.
export function isStructuralLine(line) {
  const t = line.trim();
  if (!t) return true;
  if (t.startsWith("//")) return true;
  if (/^[A-Z][a-zA-Z ]+ Prompt:\s*$/.test(t)) return true;
  return false;
}

// Walks backwards from `idx` looking for a "Negative Prompt:" header.
export function isInNegativeBlock(lines, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const t = lines[i].trim();
    if (/^[A-Z][a-zA-Z ]+ Prompt:\s*$/.test(t)) {
      return /^Negative Prompt:/i.test(t);
    }
  }
  return false;
}

// Apply rendered prompt text + structured prompt_state to a CodeMirror view
// and persist state on the node. Returns true on a successful dispatch,
// false if the view is unavailable.
export function applyPromptText(view, outputText, node, promptState) {
  if (!view || !outputText) return false;
  const source = view.state.doc.toString();
  if (outputText !== source) {
    view.dispatch({ changes: { from: 0, to: source.length, insert: outputText } });
  }
  if (node && promptState !== undefined) {
    if (!node.properties) node.properties = {};
    node.properties.pcrPromptState = promptState;
  }
  return true;
}

export function cryptoId() {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return window.crypto.randomUUID().replace(/-/g, "");
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}
