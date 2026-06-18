// Tag autocomplete — CodeMirror completion sources.
// Three parallel namespaces, one trigger for each:
//   - tagCompletionSource: booru tag CSVs (configured in tags-dropdown.js),
//     fires on plain word typing.
//   - `@`-prefixed sources for the curated PromptChain tag-builder DB:
//       atTypePickerSource — types list, fires on `@<word-chars>` (no space).
//       <type>ValueSource  — value lookup, fires on `@<type> <query>`.
//       cascadeCompletionSource — internal flow after picking a character
//                                 (auto-pops outfit then pose).
//   - `$`-prefixed: mannequinRegionSource — the 3D Poser's figures that have
//     no $name{} region block in this prompt yet; picking one inserts the
//     block with the cursor inside.

import { getTagSourceConfig } from "./tags-dropdown.js";

let _tagAbortController = null;
let _charSearchAbortController = null;
let _charDetailAbortController = null;
let _promptListAbortController = null;
const _promptCache = new Map();

// Active cascade state. Set by the character apply handler, advanced by each
// cascade stage. Cleared when the cascade finishes or is dismissed.
let _pendingCascade = null;

// ── word extraction (booru tag source) ─────────────────────────────

function getWordAtCursor(state) {
  const pos = state.selection.main.head;
  const line = state.doc.lineAt(pos);
  const before = line.text.slice(0, pos - line.from);

  // tag boundaries: commas, parens, braces
  const match = before.match(/[^,(){}]+$/);
  if (!match) return null;

  let raw = match[0].trimStart();
  // handle wildcard :: delimiter (but not single : )
  const dc = raw.lastIndexOf("::");
  if (dc !== -1) raw = raw.slice(dc + 2);
  if (!raw) return null;

  const word = raw.toLowerCase().replace(/ /g, "_");
  if (!word) return null;

  return { word, from: pos - raw.length, to: pos };
}

// ── API fetch ──────────────────────────────────────────────────────

async function fetchTags(query, limit = 20) {
  const config = getTagSourceConfig();
  if (!config.sources.length) return [];

  if (_tagAbortController) _tagAbortController.abort();
  _tagAbortController = new AbortController();

  try {
    let url;
    if (config.sources.length === 1) {
      url = `/promptchain/tags/search?source=${encodeURIComponent(config.sources[0])}&q=${encodeURIComponent(query)}&limit=${limit}`;
    } else {
      url = `/promptchain/tags/search-stacked?sources=${encodeURIComponent(config.sources.join(","))}&q=${encodeURIComponent(query)}&limit=${limit}`;
    }
    const res = await fetch(url, { signal: _tagAbortController.signal });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data) ? data : (data.tags || []);
  } catch (e) {
    if (e.name !== "AbortError") console.error("[PromptChain] Tag search error:", e);
    return [];
  }
}

async function fetchCharacters(query, limit = 10) {
  if (_charSearchAbortController) _charSearchAbortController.abort();
  _charSearchAbortController = new AbortController();

  try {
    const url = `/promptchain/tag-builder/characters?search=${encodeURIComponent(query)}&per_page=${limit}`;
    const res = await fetch(url, { signal: _charSearchAbortController.signal });
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data?.characters) ? data.characters : [];
  } catch (e) {
    if (e.name !== "AbortError") console.error("[PromptChain] Character search error:", e);
    return [];
  }
}

// Find the PromptChain node that owns this editor view. Needed because
// prompt templates are scoped to the node's current model (architecture +
// hash). Returns null when called from a fullscreen group editor (those
// aren't tracked via _pcrEditor) — caller treats that as "no prompts".
function getNodeForView(view) {
  for (const n of window.app?.graph?._nodes || []) {
    if (n._pcrEditor === view) return n;
  }
  return null;
}

function getModelInfoForView(view) {
  const node = getNodeForView(view);
  // Modal-mounted editors (inpaint/upscale) have no graph node — the mount
  // hangs a resolver directly on the view instead.
  return node?._pcrGetModelInfo?.() || view._pcrGetModelInfo?.() || null;
}

async function fetchPrompts(modelInfo) {
  const hash = modelInfo?.hash;
  if (!hash) return [];
  if (_promptCache.has(hash)) return _promptCache.get(hash);

  if (_promptListAbortController) _promptListAbortController.abort();
  _promptListAbortController = new AbortController();
  const signal = _promptListAbortController.signal;

  // Pull the model's resolved arch/family/name so the server can return
  // prompts matching all applicable scopes (architecture / family / model).
  let arch = modelInfo.architecture || "";
  let family = "";
  let name = "";
  try {
    const r = await fetch(`/promptchain/models/settings/${hash}`, { signal });
    if (r.ok) {
      const cfg = await r.json();
      arch = cfg.architecture || arch;
      family = cfg.family || "";
      name = cfg.model_name || "";
    }
  } catch (e) {
    if (e.name === "AbortError") return [];
  }

  const params = new URLSearchParams();
  if (arch) params.set("arch", arch);
  if (family) params.set("family", family);
  if (name) params.set("name", name);
  params.set("hash", hash);

  try {
    const r = await fetch(`/promptchain/prompts/list?${params}`, { signal });
    if (!r.ok) return [];
    const data = await r.json();
    const prompts = Array.isArray(data?.prompts) ? data.prompts : [];
    _promptCache.set(hash, prompts);
    return prompts;
  } catch (e) {
    if (e.name !== "AbortError") console.error("[PromptChain] Prompt fetch error:", e);
    return [];
  }
}

async function fetchCharacterDetail(tag) {
  if (_charDetailAbortController) _charDetailAbortController.abort();
  _charDetailAbortController = new AbortController();

  try {
    const url = `/promptchain/tag-builder/characters/${encodeURIComponent(tag)}`;
    const res = await fetch(url, { signal: _charDetailAbortController.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    if (e.name !== "AbortError") console.error("[PromptChain] Character detail error:", e);
    return null;
  }
}

// ── formatting ─────────────────────────────────────────────────────

function formatForInsertion(tag, format) {
  let result = tag;
  // escape parens for SD attention syntax
  result = result.replace(/\(/g, "\\(").replace(/\)/g, "\\)");
  if (format === "spaces") result = result.replace(/_/g, " ");
  return result;
}

function formatForDisplay(tag, format) {
  return format === "spaces" ? tag.replace(/_/g, " ") : tag;
}

// base_tags / outfit_tags / pose_tags are stored as comma-separated tag
// lists that already include intentional SD weight syntax like (cammy_white:1.1).
// Those parens are meaningful — escaping them would break the weight. Only
// substitute underscores when the user has chosen "spaces" formatting.
function formatBodyTagsForInsertion(body, format) {
  if (!body) return "";
  return format === "spaces" ? body.replace(/_/g, " ") : body;
}

function formatPopularity(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return n > 0 ? `${n}` : "";
}

// ── completion options ─────────────────────────────────────────────

function toCompletionOptions(tags) {
  const config = getTagSourceConfig();
  return tags.map((tag) => {
    const name = tag.tag || "";
    const display = formatForDisplay(name, config.format);
    const insert = formatForInsertion(name, config.format);
    const pop = formatPopularity(tag.ranking || 0);
    const detail = [pop, tag.source].filter(Boolean).join(" · ");

    return {
      label: name.toLowerCase(),
      displayLabel: display,
      apply: insert,
      detail,
      type: tag.category || "general",
      boost: (tag.ranking || 0) / 10_000_000,
    };
  });
}

function buildCharacterInsertion(char, config) {
  const display = char.display || (char.tag || "").replace(/_/g, " ");
  const seriesSuffix = char.series ? ` (${char.series})` : "";
  const header = `// Character: ${display}${seriesSuffix}`;

  const body = config.prompt_style === "natural"
    ? (char.base_natlang || "").trim()
    : formatBodyTagsForInsertion((char.base_tags || "").trim(), config.format);

  return body ? `${header}\n${body}\n\n` : `${header}\n\n`;
}

function buildOutfitInsertion(outfit, characterDisplay, config) {
  const name = outfit.outfit_name || "outfit";
  const flag = outfit.is_default ? " (default)" : "";
  const header = `// Outfit: ${name}${flag} from Character: ${characterDisplay}`;

  const body = config.prompt_style === "natural"
    ? (outfit.outfit_natlang || "").trim()
    : formatBodyTagsForInsertion((outfit.outfit_tags || "").trim(), config.format);

  return body ? `${header}\n${body}\n\n` : `${header}\n\n`;
}

// Returns {text, cursorOffset} where text is the full insertion and
// cursorOffset is where the editor cursor should land within it.
//
// Two modes, branching on whether the editor already has prompt content
// outside the `@prompt <query>` we're about to replace:
//
// EMPTY mode — full template with `// Prompt:` comment header. Template's
//   `{cursor}` token marks where user tags go; real cursor lands there.
//
// MERGE mode — the existing content IS the user's tags, so the template's
//   user-tags slot (everything up to and including `{cursor}`) and the
//   `// Prompt:` header are dropped. Only the style + negative section is
//   appended. Cursor lands at the end of the positive section (right before
//   `Negative Prompt:`), or end of insertion if there's no negative block.
//   A separator of 2 newlines is prepended unless the existing content
//   already ends with enough newlines.
function buildPromptInsertion(prompt, existingBefore, existingAfter) {
  const category = (prompt.category || "").trim();
  const name = (prompt.name || "prompt").trim();
  const title = category ? `${category} > ${name}` : name;

  const text = prompt.text || "";
  const cursorIdx = text.indexOf("{cursor}");
  const hasExistingContent = (existingBefore + existingAfter).trim().length > 0;

  if (!hasExistingContent) {
    const header = `// Prompt: ${title}\n`;
    if (cursorIdx === -1) {
      const insert = `${header}${text}\n\n`;
      return { text: insert, cursorOffset: insert.length };
    }
    const before = text.slice(0, cursorIdx);
    const after = text.slice(cursorIdx + "{cursor}".length);
    const insert = `${header}${before}${after}\n\n`;
    return { text: insert, cursorOffset: header.length + before.length };
  }

  const after = cursorIdx >= 0 ? text.slice(cursorIdx + "{cursor}".length) : text;
  const trimmedAfter = after.replace(/^\s+/, "");

  const negMatch = trimmedAfter.match(/\n+Negative Prompt:/i);
  let cursorWithinAfter;
  if (negMatch) {
    let idx = negMatch.index;
    while (idx > 0 && /\s/.test(trimmedAfter[idx - 1])) idx--;
    cursorWithinAfter = idx;
  } else {
    cursorWithinAfter = trimmedAfter.length;
  }

  const trailNewlines = (existingBefore.match(/\n*$/) || [""])[0].length;
  const separator = "\n".repeat(Math.max(0, 2 - trailNewlines));
  return {
    text: separator + trimmedAfter,
    cursorOffset: separator.length + cursorWithinAfter,
  };
}

function buildPoseInsertion(pose, characterDisplay, config) {
  const name = pose.pose_name || "pose";
  const flag = pose.is_signature ? " (signature)" : "";
  const header = `// Pose: ${name}${flag} from Character: ${characterDisplay}`;

  const body = config.prompt_style === "natural"
    ? (pose.pose_natlang || "").trim()
    : formatBodyTagsForInsertion((pose.pose_tags || "").trim(), config.format);

  return body ? `${header}\n${body}\n\n` : `${header}\n\n`;
}

// ── cascade orchestration ─────────────────────────────────────────

function endCascade() {
  if (_pendingCascade?.escHandler && _pendingCascade?.view) {
    _pendingCascade.view.contentDOM.removeEventListener(
      "keydown", _pendingCascade.escHandler, true,
    );
  }
  _pendingCascade = null;
}

function attachCascadeEscHandler(view) {
  if (!_pendingCascade || _pendingCascade.escHandler) return;
  const handler = (e) => {
    if (e.key !== "Escape" || !_pendingCascade) return;
    endCascade();
  };
  view.contentDOM.addEventListener("keydown", handler, true);
  _pendingCascade.view = view;
  _pendingCascade.escHandler = handler;
}

// CM6's acceptCompletion auto-attaches the pickedCompletion annotation when
// `apply` is a string, but not when it's a function — without that annotation
// the popup state never marks the completion as picked, which leaves the
// dropdown alive across our cascade transitions and lets it reappear instead
// of closing on Tab / (skip).
function dispatchWithPicked(view, completion, txn) {
  const ann = window.PromptChainCM?.pickedCompletion?.of?.(completion);
  view.dispatch(ann ? { ...txn, annotations: ann } : txn);
}

function triggerCompletion(view, delay = 50) {
  setTimeout(() => {
    const CM = window.PromptChainCM;
    if (!CM?.startCompletion) return;
    CM.startCompletion(view);
  }, delay);
}

function triggerCascadePopup(view) {
  attachCascadeEscHandler(view);
  triggerCompletion(view, 50);
}

function advanceCascade(view) {
  if (!_pendingCascade) return;
  if (_pendingCascade.stage === "outfit") {
    _pendingCascade.stage = "pose";
    triggerCascadePopup(view);
  } else {
    endCascade();
  }
}

// ── completion sources ────────────────────────────────────────────

async function tagCompletionSource(context) {
  const info = getWordAtCursor(context.state);
  if (!info || info.word.length < 2) return null;
  // `@`-prefixed prefixes belong to the @ source family — booru tag DB must
  // not also fire on them, otherwise the two namespaces collide. Same for
  // `$` anywhere in the run: that's a region block being typed (booru tags
  // never contain `$`).
  if (info.word.startsWith("@") || info.word.includes("$")) return null;

  const tags = await fetchTags(info.word, 20);
  if (!tags.length) return null;

  return {
    from: info.from,
    to: info.to,
    options: toCompletionOptions(tags),
    filter: false,
  };
}

// Type-picker dropdown — fires on `@<word-chars>` (no space). Lists every
// available `@`-shortcut so users can discover them. Selecting an entry
// inserts `@<type> ` and re-triggers autocomplete to drop into the type's
// value-search dropdown.
function atTypePickerSource(context) {
  const m = context.matchBefore(/@\w*$/);
  if (!m) return null;

  // Filter at the source level by typed prefix (case-insensitive). CM6's
  // built-in filter still keeps options that share the leading `@` even when
  // the rest of the chars don't match, so `@prompt` was leaving `@character`
  // visible and pre-selected. Empty typed text → show every type (discovery).
  const typed = m.text.slice(1).toLowerCase();
  const matches = typed
    ? AT_TYPES.filter((t) => t.name.toLowerCase().startsWith(typed))
    : AT_TYPES;
  if (!matches.length) return null;

  return {
    from: m.from,
    to: m.to,
    options: matches.map((t) => ({
      label: `@${t.name}`,
      displayLabel: `@${t.name}`,
      detail: t.description,
      type: "namespace",
      apply: (view, completion, from, to) => {
        const insert = `@${t.name} `;
        dispatchWithPicked(view, completion, {
          changes: { from, to, insert },
          selection: { anchor: from + insert.length },
        });
        triggerCompletion(view, 50);
      },
    })),
    filter: false,
  };
}

// ── $-region source (3D Poser mannequins) ───────────────────────────

// The Poser whose figures this prompt's $blocks bind to: trace the node's
// regions output -> AttentionCouple -> its masks input -> Poser. Falls back to
// any live Poser in the graph so the dropdown works before regional is wired.
function getPoserForView(view) {
  const graph = window.app?.graph;
  const node = getNodeForView(view);
  if (!graph || !node) return null;
  const linkOf = (id) => graph.links?.get?.(id) || graph.links?.[id] || null;
  const regOut = node.outputs?.find((o) => o.name === "regions");
  for (const linkId of regOut?.links || []) {
    const link = linkOf(linkId);
    const couple = link && graph.getNodeById(link.target_id);
    if (couple?.comfyClass !== "PromptChain_AttentionCouple") continue;
    const maskIn = couple.inputs?.find((i) => i.name === "masks");
    const mlink = maskIn?.link != null ? linkOf(maskIn.link) : null;
    const poser = mlink && graph.getNodeById(mlink.origin_id);
    if (poserHasEntities(poser)) return poser;
  }
  return (graph._nodes || []).find(
    (n) => n.comfyClass === "PromptChain_PoseStudio" && poserHasEntities(n)) || null;
}

// A poser is listable if it has ANY region entity — figures, or (figure-less
// scenes are legal now) named props via entityNames.
function poserHasEntities(poser) {
  const ps = poser?._pcrPose;
  if (!ps) return false;
  return ps.entityNames ? ps.entityNames().length > 0 : !!ps.figures?.length;
}

// Fires on `$<word-chars>`: lists the Poser's figures that have no $name{}
// block in this prompt yet (assigned ones are hidden — the dropdown is for
// quickly attaching the rest of a large cast). Picking one inserts the block
// and parks the cursor inside it.
function mannequinRegionSource(context) {
  const m = context.matchBefore(/\$\w*$/);
  if (!m || !context.view) return null;
  const ps = getPoserForView(context.view)?._pcrPose;
  // Region entities = figures + named props (entityNames mirrors the Poser's
  // mask-row names); pre-entity builds fall back to the figure list.
  const names = ps?.entityNames
    ? ps.entityNames()
    : ps?.figures?.map((f, i) => f.customName || "mannequin" + (i + 1));
  if (!names?.length) return null;

  const doc = context.state.doc.toString();
  const typed = m.text.slice(1).toLowerCase();
  const unassigned = names
    .filter((name) =>
      !new RegExp("\\$" + name + "\\s*\\{", "i").test(doc) &&
      (!typed || name.toLowerCase().startsWith(typed)));
  if (!unassigned.length) return null;

  return {
    from: m.from,
    to: m.to,
    options: unassigned.map((name) => ({
      label: "$" + name,
      displayLabel: "$" + name,
      detail: "add this entity's region block",
      type: "namespace",
      apply: (view, completion, from, to) => {
        const insert = "$" + name + " {\n\n}";
        dispatchWithPicked(view, completion, {
          changes: { from, to, insert },
          selection: { anchor: from + name.length + 4 }, // on the blank line inside the braces
        });
      },
    })),
    filter: false,
  };
}

async function characterValueSource(context) {
  const m = context.matchBefore(/@character\s+([^,\n]*)$/);
  if (!m) return null;
  const queryMatch = m.text.match(/@character\s+([^,\n]*)$/);
  const query = (queryMatch?.[1] || "").trim();

  const chars = await fetchCharacters(query, 10);
  if (!chars.length) return null;

  const config = getTagSourceConfig();
  return {
    from: m.from,
    to: m.to,
    options: chars.map((c) => ({
      label: c.tag,
      displayLabel: c.display || c.tag,
      detail: c.series || c.tag,
      type: "character",
      apply: (view, completion, from, to) => {
        const insert = buildCharacterInsertion(c, config);
        dispatchWithPicked(view, completion, {
          changes: { from, to, insert },
          selection: { anchor: from + insert.length },
        });
        endCascade();
        _pendingCascade = {
          stage: "outfit",
          characterTag: c.tag,
          characterDisplay: c.display || (c.tag || "").replace(/_/g, " "),
          detail: null,
          config,
          view: null,
          escHandler: null,
          expectedPos: from + insert.length,
        };
        triggerCascadePopup(view);
      },
    })),
    filter: false,
  };
}

async function promptValueSource(context) {
  const m = context.matchBefore(/@prompt\s+([^,\n]*)$/);
  if (!m) return null;
  const queryMatch = m.text.match(/@prompt\s+([^,\n]*)$/);
  const query = (queryMatch?.[1] || "").trim().toLowerCase();

  const modelInfo = getModelInfoForView(context.view);
  if (!modelInfo) return null;

  const prompts = await fetchPrompts(modelInfo);
  if (!prompts.length) return null;

  const filtered = query
    ? prompts.filter((p) =>
        (p.name || "").toLowerCase().includes(query) ||
        (p.category || "").toLowerCase().includes(query))
    : prompts;
  if (!filtered.length) return null;

  return {
    from: m.from,
    to: m.to,
    options: filtered.map((p) => ({
      label: p.id || p.name || "prompt",
      displayLabel: (p.category ? `${p.category} > ` : "") + (p.name || "prompt"),
      detail: "prompt template",
      type: "prompt",
      apply: (view, completion, from, to) => {
        const existingBefore = view.state.doc.sliceString(0, from);
        const existingAfter = view.state.doc.sliceString(to, view.state.doc.length);
        const { text, cursorOffset } = buildPromptInsertion(p, existingBefore, existingAfter);
        dispatchWithPicked(view, completion, {
          changes: { from, to, insert: text },
          selection: { anchor: from + cursorOffset },
        });
      },
    })),
    filter: false,
  };
}

async function cascadeCompletionSource(context) {
  if (!_pendingCascade) return null;
  // Cascade is anchored to where the previous stage's apply left the cursor.
  // If the cursor has moved (user typed @ to start a fresh lookup, clicked
  // away, used arrow keys, etc.) the cascade is stale — terminate so the
  // dropdown doesn't co-render alongside the type-picker or whatever else.
  if (_pendingCascade.expectedPos != null && context.pos !== _pendingCascade.expectedPos) {
    endCascade();
    return null;
  }
  const { stage, characterTag, characterDisplay, config } = _pendingCascade;

  if (!_pendingCascade.detail) {
    _pendingCascade.detail = await fetchCharacterDetail(characterTag);
    if (!_pendingCascade) return null;
  }

  const detail = _pendingCascade.detail;
  if (!detail) {
    _pendingCascade = null;
    return null;
  }

  const items = stage === "outfit" ? (detail.outfits || []) : (detail.poses || []);
  const buildFn = stage === "outfit" ? buildOutfitInsertion : buildPoseInsertion;
  const nameKey = stage === "outfit" ? "outfit_name" : "pose_name";

  if (!items.length) {
    advanceCascade(context.view ?? null);
    return null;
  }

  const pos = context.pos;
  const skipOption = {
    label: `(skip ${stage})`,
    displayLabel: `(skip ${stage})`,
    detail: "advance without inserting",
    type: "text",
    boost: 99,
    apply: (view, completion, from, to) => {
      dispatchWithPicked(view, completion, { changes: { from, to, insert: "" } });
      advanceCascade(view);
    },
  };

  const itemOptions = items.map((it) => ({
    label: it[nameKey] || stage,
    displayLabel: it[nameKey] || stage,
    detail: stage === "outfit"
      ? (it.is_default ? "default" : "outfit")
      : (it.is_signature ? "signature" : "pose"),
    type: stage,
    apply: (view, completion, from, to) => {
      const insert = buildFn(it, characterDisplay, config);
      dispatchWithPicked(view, completion, {
        changes: { from, to, insert },
        selection: { anchor: from + insert.length },
      });
      // Update expectedPos so the next stage's cascade source matches the
      // post-insertion cursor — without this, advancing outfit→pose would
      // immediately fail the position check and terminate.
      if (_pendingCascade) _pendingCascade.expectedPos = from + insert.length;
      advanceCascade(view);
    },
  }));

  return {
    from: pos,
    to: pos,
    options: [skipOption, ...itemOptions],
    filter: false,
  };
}

// ── @-shortcut registry ───────────────────────────────────────────
//
// Add a new top-level @-shortcut by appending an entry here and providing
// its value source in the source list passed to activateTagAutocomplete.

const AT_TYPES = [
  {
    name: "character",
    description: "PromptChain character bio (auto-cascades to outfit + pose)",
  },
  {
    name: "prompt",
    description: "Prompt template scoped to the current model",
  },
  // future:
  //   { name: "prop", description: "prop / clothing / fantasy / furniture" },
];

// ── activation ─────────────────────────────────────────────────────

// `@` and `$` are not word characters, so CM6's activateOnTyping won't
// auto-fire their dropdowns. Hook keydown so the type-picker / unassigned-
// mannequin list appears the instant either is typed.
function installAtTrigger(view) {
  if (view._pcrAtTriggerInstalled) return;
  view._pcrAtTriggerInstalled = true;
  view.contentDOM.addEventListener("keydown", (e) => {
    if (e.key !== "@" && e.key !== "$") return;
    triggerCompletion(view, 0);
  });
}

/**
 * Reconfigure the editor's autocomplete compartment to include tag completion
 * alongside the existing wildcard completion. Call after editor is created.
 */
export function activateTagAutocomplete(CM, view) {
  CM.reconfigureAutocomplete(view, [
    CM.wildcardCompletionSource,
    cascadeCompletionSource,
    characterValueSource,
    promptValueSource,
    atTypePickerSource,
    mannequinRegionSource,
    tagCompletionSource,
  ]);
  installAtTrigger(view);
}
