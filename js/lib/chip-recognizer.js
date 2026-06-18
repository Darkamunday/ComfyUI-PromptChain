// CodeMirror extension: highlight recognized chip natlangs in the doc
// with a subtle status-colored underline + on-hover tooltip showing the
// chip's display name, bucket, tag, natlang, and current QA status.
//
// Read-only annotation: nothing about the text changes — just a styled
// mark span underneath, so the editor stays a normal text editor and
// selection / cursor behavior is unaffected.
//
// Architecture: decorations live in a StateField (canonical CM6 pattern
// for async-computed decorations). The ViewPlugin watches doc changes,
// debounces a rescan, and dispatches a setDecorations effect that the
// field consumes. tr.changes mapping keeps decorations anchored as the
// user types between rescans.

import { loadChipIndex, getChipIndex, onChipIndexReady } from "./chip-index.js";

const BOUNDARY_RE = /[.,;\s]/;
const BASE_CLASS = "pcr-chip-recognized";
const DEBOUNCE_MS = 150;

function statusClass(status) {
  if (status === "normalized") return "pcr-chip-recognized-ready";
  if (status === "broken") return "pcr-chip-recognized-broken";
  return "pcr-chip-recognized-unprocessed";
}

// Reverse the server's hair-length-onto-style weld. compose_character_
// natlang_v2 prefixes a hair_length adjective onto the primary hair_style
// phrase ("long" + "twin braids" -> "long twin braids"). Given that
// welded phrase, recover the two member chips so the span shows both on
// hover. Longest style suffix wins (drop leading words until the
// remainder is a known hair_style and the prefix is a known length
// adjective). Returns [lengthInfo, styleInfo] or null.
function decomposeHairWeld(phrase, index) {
  if (!index || !index.hairStyleByPhrase || !index.hairLengthAdj) return null;
  const words = phrase.toLowerCase().split(/\s+/);
  if (words.length < 2) return null;
  for (let i = 1; i < words.length; i++) {
    const prefix = words.slice(0, i).join(" ");
    const suffix = words.slice(i).join(" ");
    const styleInfo = index.hairStyleByPhrase.get(suffix);
    const lengthInfo = index.hairLengthAdj.get(prefix);
    if (styleInfo && lengthInfo) return [lengthInfo, styleInfo];
  }
  return null;
}

// Walks the doc text, emits {from, to, info} for every recognized chip
// natlang. Two-pass: multi-comma natlangs first (longest-first, claim
// spans), then single-phrase comma-chunked lookup over the leftovers.
function scanChips(text, index) {
  if (!text || !index) return [];
  const matches = [];
  const taken = []; // [start, end] intervals claimed by multi-comma pass

  // Helper: longest-first phrase pass against the full text, claiming
  // non-overlapping spans into `taken` so a later pass doesn't reclaim
  // already-matched ranges.
  const claimPass = (entries) => {
    for (const entry of entries) {
      let from = 0;
      while (from <= text.length) {
        const at = text.indexOf(entry.natlang, from);
        if (at < 0) break;
        const end = at + entry.natlang.length;
        const before = at === 0 ? "" : text.charAt(at - 1);
        const after = end >= text.length ? "" : text.charAt(end);
        const okBefore = at === 0 || BOUNDARY_RE.test(before);
        const okAfter = end >= text.length || BOUNDARY_RE.test(after);
        let overlap = false;
        for (const [s, e] of taken) {
          if (at < e && end > s) { overlap = true; break; }
        }
        if (okBefore && okAfter && !overlap) {
          matches.push({ from: at, to: end, info: entry.info });
          taken.push([at, end]);
          from = end;
        } else {
          from = at + 1;
        }
      }
    }
  };

  // Characters first — a character base_natlang ("Cammy White from
  // Street Fighter") is a strong identity match; if it appears anywhere
  // the user means the character, not coincidental words.
  claimPass(index.characterList || []);
  // Override (enhancer) phrasings next — a character-specific enhancer
  // like Cammy's verbose scar description is more specific than the
  // generic chip phrase, so it should claim its span first.
  claimPass(index.overrideList || []);
  // Then multi-comma chips so a long armor description claims its full
  // span before single-phrase pieces light up individually.
  claimPass(index.multiCommaList);

  // Weighted tokens — "(close-up:1.2)". The chunk splitter below excludes
  // periods, so the decimal weight shreds the token and it never forms a
  // clean chunk. Match these explicitly and look up the inner tag, then
  // claim the inner span so the chunk pass doesn't reprocess the pieces.
  const WEIGHT_RE = /\(\s*([^():]+?)\s*:\s*[\d.]+\s*\)/g;
  let wm;
  while ((wm = WEIGHT_RE.exec(text)) !== null) {
    const inner = wm[1].trim();
    if (!inner) continue;
    const innerStart = wm.index + wm[0].indexOf(inner);
    const innerEnd = innerStart + inner.length;
    let overlap = false;
    for (const [s, e] of taken) {
      if (innerStart < e && innerEnd > s) { overlap = true; break; }
    }
    if (overlap) continue;
    const info = index.phraseToChip.get(inner) || index.tagToChip?.get(inner.toLowerCase());
    if (info) {
      matches.push({ from: innerStart, to: innerEnd, info });
      taken.push([innerStart, innerEnd]);
    }
  }

  // Walk every comma/period/newline-delimited chunk and look the
  // trimmed phrase up in the single-phrase map. Skip chunks fully
  // inside a multi-comma claim so a "metal helmet" inside an armor
  // span doesn't double-light.
  const chunkRe = /([^,.\n]+)(?=,|\.\s|\.$|\n|$)/g;
  let m;
  while ((m = chunkRe.exec(text)) !== null) {
    const raw = m[0];
    const rawStart = m.index;
    const phrase = raw.trim();
    if (!phrase) {
      if (chunkRe.lastIndex === m.index) chunkRe.lastIndex++;
      continue;
    }
    const phraseStart = rawStart + raw.indexOf(phrase);
    const phraseEnd = phraseStart + phrase.length;

    let inside = false;
    for (const [s, e] of taken) {
      if (phraseStart >= s && phraseEnd <= e) { inside = true; break; }
    }
    if (inside) continue;

    // Natlang phrase first, then tag form (so tag-mode tokens like
    // "double bun" or "heart-shaped pupils" resolve even when their
    // base_natlang differs from the tag).
    const info = index.phraseToChip.get(phrase) || index.tagToChip?.get(phrase.toLowerCase());
    if (info) {
      matches.push({ from: phraseStart, to: phraseEnd, info });
      continue;
    }
    // No whole-phrase chip: a welded hair feature ("long twin braids")
    // reads as one chunk but maps to two canonical tags. Decompose it so
    // both chips stay hoverable from the single span.
    const members = decomposeHairWeld(phrase, index);
    if (members) {
      matches.push({ from: phraseStart, to: phraseEnd, members });
    }
  }

  matches.sort((a, b) => a.from - b.from);
  return matches;
}

function buildDecorations(CM, matches) {
  const decs = [];
  for (const { from, to, info, members } of matches) {
    // Welded multi-chip span (e.g. "long twin braids" → long_hair +
    // twin_braids). One decoration, both tags in a CSV attribute; the
    // hover handler fans them back out into a stacked tooltip. Color
    // follows the style chip's status since that's the visible feature.
    if (members && members.length) {
      const style = members[members.length - 1];
      decs.push(
        CM.Decoration.mark({
          class: `${BASE_CLASS} ${statusClass(style.natlang_status)} pcr-chip-bucket-appearance`,
          attributes: {
            "data-pcr-chip-tags": members.map((m) => m.item_tag).join(","),
            "data-pcr-chip-bucket": "appearance",
          },
        }).range(from, to)
      );
      continue;
    }
    // Pill color follows override_status when the match came from an
    // override phrasing — the user's intent is to A/B the enhancer
    // independently of the base chip natlang's status.
    const statusForColor = info.override_scope
      ? info.override_status
      : info.natlang_status;
    const attrs = {
      "data-pcr-chip-tag": info.item_tag,
      "data-pcr-chip-bucket": info.bucket,
    };
    if (info.override_scope) {
      attrs["data-pcr-chip-override-scope"] = info.override_scope;
      attrs["data-pcr-chip-override-id"] = String(info.override_scope_id);
    }
    decs.push(
      CM.Decoration.mark({
        class: `${BASE_CLASS} ${statusClass(statusForColor)} pcr-chip-bucket-${info.bucket}`,
        attributes: attrs,
      }).range(from, to)
    );
  }
  return CM.Decoration.set(decs, true);
}

// Shared floating tooltip DOM. Reused across editors. Anchored to
// document.body so it escapes overflow:hidden ancestors on node UI.
let tooltipEl = null;
function ensureTooltip() {
  if (tooltipEl) return tooltipEl;
  tooltipEl = document.createElement("div");
  tooltipEl.className = "pcr-chip-tooltip";
  tooltipEl.style.display = "none";
  document.body.appendChild(tooltipEl);
  return tooltipEl;
}
function hideTooltip() {
  if (tooltipEl) tooltipEl.style.display = "none";
  tooltipKey = null;
}
// Tracks which chip the tooltip currently represents so we can skip
// the expensive DOM rebuild + image load on every mousemove pixel and
// just reposition. Cleared in hideTooltip / when the cursor leaves a
// chip.
let tooltipKey = null;

function positionTooltip(x, y) {
  const el = ensureTooltip();
  el.style.left = `${x + 12}px`;
  el.style.top = `${y + 18}px`;
  el.style.display = "block";
  const rect = el.getBoundingClientRect();
  if (rect.right > window.innerWidth) {
    el.style.left = `${Math.max(8, window.innerWidth - rect.width - 8)}px`;
  }
  if (rect.bottom > window.innerHeight) {
    el.style.top = `${Math.max(8, y - rect.height - 12)}px`;
  }
}

function renderTooltip(info, x, y) {
  const el = ensureTooltip();
  el.replaceChildren();

  // Thumbnail at the top. Cast thumbs are served under the cast group
  // (item_group), not the "cast" bucket name; everything else uses the
  // bucket. Missing thumbs 404 silently via onerror.
  const thumbSeg = info.bucket === "cast" ? info.item_group : info.bucket;
  if (thumbSeg) {
    const thumb = document.createElement("img");
    thumb.className = "pcr-chip-tooltip-thumb";
    thumb.src = `/promptchain/tag-builder/thumb/${encodeURIComponent(thumbSeg)}/${encodeURIComponent(info.item_tag)}`;
    thumb.onerror = () => thumb.remove();
    el.appendChild(thumb);
  }

  const title = document.createElement("div");
  title.className = "pcr-chip-tooltip-title";
  title.textContent = info.display_name || info.item_tag;
  el.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "pcr-chip-tooltip-sub";
  sub.textContent = `${info.bucket}${info.item_group ? " · " + info.item_group : ""}`;
  el.appendChild(sub);

  // Every chip gets the standard Tag/Natlang label-value rows. Migrated
  // character chips append a Base Appearance pill grid below so the user
  // sees the bound appearance chips at a glance alongside the canonical
  // tag/natlang display.
  renderTagRow(el, info);
  renderNatlangRow(el, info);

  if (info.override_scope) {
    renderEnhancerRow(el, info);
  }

  if (info.bucket === "characters" && info.migrated && info.appearance_chips?.length) {
    renderBaseAppearanceSection(el, info);
  }

  // Status pill at the bottom. Override matches show the override's
  // own status pill since that's what's authoritative for the matched
  // text; non-override matches show the base chip's natlang_status.
  const status = document.createElement("div");
  if (info.override_scope) {
    status.className = `pcr-chip-tooltip-status pcr-chip-status-${info.override_status || "unprocessed"}`;
    status.textContent = `enhancer · ${info.override_status || "unprocessed"}`;
  } else {
    status.className = `pcr-chip-tooltip-status pcr-chip-status-${info.natlang_status || "unprocessed"}`;
    status.textContent = info.natlang_status || "unprocessed";
  }
  el.appendChild(status);

  positionTooltip(x, y);
}

// Tooltip for a welded multi-chip span. One phrase ("long twin braids")
// stands in for several canonical tags; show each member so the user sees
// both the prose feature and the tags it expands back into.
function renderCompositeTooltip(infos, x, y) {
  const el = ensureTooltip();
  el.replaceChildren();

  const title = document.createElement("div");
  title.className = "pcr-chip-tooltip-title";
  title.textContent = infos.map((i) => i.display_name || i.item_tag).join(" + ");
  el.appendChild(title);

  const sub = document.createElement("div");
  sub.className = "pcr-chip-tooltip-sub";
  sub.textContent = "appearance · combined";
  el.appendChild(sub);

  // Canonical tags this single phrase expands back into on tag-mode.
  const tagRow = document.createElement("div");
  tagRow.className = "pcr-chip-tooltip-row";
  const tagLabel = document.createElement("span");
  tagLabel.className = "pcr-chip-tooltip-label";
  tagLabel.textContent = "Tags";
  const tagValue = document.createElement("span");
  tagValue.className = "pcr-chip-tooltip-value pcr-chip-tooltip-mono";
  tagValue.textContent = infos.map((i) => i.base_tags || i.item_tag).join(", ");
  tagRow.appendChild(tagLabel);
  tagRow.appendChild(tagValue);
  el.appendChild(tagRow);

  for (const info of infos) {
    const header = document.createElement("div");
    header.className = "pcr-chip-tooltip-section-header";
    header.textContent = `${info.display_name || info.item_tag}${info.item_group ? " · " + info.item_group : ""}`;
    el.appendChild(header);
    renderNatlangRow(el, info);
    const status = document.createElement("div");
    status.className = `pcr-chip-tooltip-status pcr-chip-status-${info.natlang_status || "unprocessed"}`;
    status.textContent = info.natlang_status || "unprocessed";
    el.appendChild(status);
  }

  positionTooltip(x, y);
}

function renderEnhancerRow(el, info) {
  // Scope label reads "Cammy White" for character overrides and
  // "Delta Red (Cammy White)" for outfit/pose overrides — the
  // character context disambiguates same-named outfits across roster.
  const scopeLabel = info.override_scope_character_display
    ? `${info.override_scope_display} (${info.override_scope_character_display})`
    : info.override_scope_display;

  const header = document.createElement("div");
  header.className = "pcr-chip-tooltip-section-header";
  header.textContent = `Enhancer · ${info.override_scope} · ${scopeLabel}`;
  el.appendChild(header);

  const row = document.createElement("div");
  row.className = "pcr-chip-tooltip-enhancer";
  row.textContent = info.override_natlang;
  el.appendChild(row);
}

function renderTagRow(el, info) {
  const tagRow = document.createElement("div");
  tagRow.className = "pcr-chip-tooltip-row";
  const tagLabel = document.createElement("span");
  tagLabel.className = "pcr-chip-tooltip-label";
  tagLabel.textContent = "Tag";
  const tagValue = document.createElement("span");
  tagValue.className = "pcr-chip-tooltip-value pcr-chip-tooltip-mono";
  tagValue.textContent = info.base_tags || info.item_tag;
  tagRow.appendChild(tagLabel);
  tagRow.appendChild(tagValue);
  el.appendChild(tagRow);
}

function renderNatlangRow(el, info) {
  const nlRow = document.createElement("div");
  nlRow.className = "pcr-chip-tooltip-row";
  const nlLabel = document.createElement("span");
  nlLabel.className = "pcr-chip-tooltip-label";
  nlLabel.textContent = "Natlang";
  const nlValue = document.createElement("span");
  nlValue.className = "pcr-chip-tooltip-value";
  nlValue.textContent = info.base_natlang || "(empty)";
  nlRow.appendChild(nlLabel);
  nlRow.appendChild(nlValue);
  el.appendChild(nlRow);
}

function renderBaseAppearanceSection(el, info) {
  if (!info.appearance_chips?.length) return;
  const header = document.createElement("div");
  header.className = "pcr-chip-tooltip-section-header";
  header.textContent = "Base appearance";
  el.appendChild(header);

  const grid = document.createElement("div");
  grid.className = "pcr-chip-tooltip-pill-grid";
  for (const chip of info.appearance_chips) {
    const pill = document.createElement("span");
    pill.className = `pcr-chip-tooltip-pill pcr-chip-tooltip-pill-${chip.natlang_status || "unprocessed"}`;
    pill.textContent = chip.display_name || chip.item_tag;
    pill.title = `${chip.item_tag} — ${chip.base_natlang || "(no natlang)"}`;
    grid.appendChild(pill);
  }
  el.appendChild(grid);
}

// Quick reverse-lookup helper. We have the tag+bucket on the DOM via
// data attributes; the index is keyed by natlang phrase, so we scan
// the values to find the chip. 5k items is cheap enough on a single
// hover that a separate reverse index isn't worth the memory.
//
// `overrideFor` (optional) selects the override entry for the given
// character_tag instead of the generic chip — the tooltip needs the
// override metadata (enhancer text, status) to render its Enhancer
// section.
function findChipByTag(index, tag, bucket, overrideScope, overrideId) {
  if (!index || !tag) return null;
  if (overrideScope && overrideId !== null && overrideId !== undefined) {
    for (const entry of index.overrideList || []) {
      if (entry.info.item_tag === tag
          && entry.info.override_scope === overrideScope
          && String(entry.info.override_scope_id) === String(overrideId)) {
        return entry.info;
      }
    }
    // Fall through to generic lookup if the override row got deleted
    // between scan and hover (rare race).
  }
  if (bucket === "characters") {
    for (const entry of index.characterList || []) {
      if (entry.info.item_tag === tag) return entry.info;
    }
    return null;
  }
  for (const v of index.phraseToChip.values()) {
    if (v.item_tag === tag && v.bucket === bucket) return v;
  }
  for (const entry of index.multiCommaList) {
    if (entry.info.item_tag === tag && entry.info.bucket === bucket) return entry.info;
  }
  return null;
}

// Builds the extension. Returns a CM extension array suitable to spread
// into editor.js's createEditor extensions list.
export function chipRecognizerExtension(CM) {
  loadChipIndex();

  // Effect dispatched by the plugin when a fresh scan completes.
  const setDecorationsEffect = CM.StateEffect.define();

  // Decorations live in a StateField so they survive between plugin
  // ticks and get re-mapped through doc changes (so a mark at position
  // 50 stays on the same text after a 5-char insert at position 10).
  const decorationsField = CM.StateField.define({
    create: () => CM.Decoration.none,
    update(value, tr) {
      let v = value.map(tr.changes);
      for (const ef of tr.effects) {
        if (ef.is(setDecorationsEffect)) v = ef.value;
      }
      return v;
    },
    provide: f => CM.EditorView.decorations.from(f),
  });

  const plugin = CM.ViewPlugin.fromClass(class {
    constructor(view) {
      this.view = view;
      this.timer = null;
      this.schedule();
      this.unsubscribe = onChipIndexReady(() => this.schedule());
    }
    update(update) {
      if (update.docChanged) this.schedule();
    }
    schedule() {
      if (this.timer) clearTimeout(this.timer);
      this.timer = setTimeout(() => {
        this.timer = null;
        const index = getChipIndex();
        const text = this.view.state.doc.toString();
        const matches = index ? scanChips(text, index) : [];
        const decs = buildDecorations(CM, matches);
        this.view.dispatch({ effects: setDecorationsEffect.of(decs) });
      }, DEBOUNCE_MS);
    }
    destroy() {
      if (this.timer) clearTimeout(this.timer);
      this.unsubscribe?.();
    }
  });

  // Mousemove fires per pixel; rebuilding the tooltip on every event
  // (which includes a new img element and image load) is what caused
  // the flicker. Track which chip the tooltip currently represents and
  // skip the rebuild when cursor stays inside the same chip span —
  // just reposition.
  const tooltipHandlers = CM.EditorView.domEventHandlers({
    mousemove: (event) => {
      const target = event.target.closest(`.${BASE_CLASS}`);
      if (!target) { hideTooltip(); return false; }
      // Welded multi-chip span: render every member stacked so both
      // canonical tags behind the one phrase are visible.
      const multiTags = target.getAttribute("data-pcr-chip-tags");
      if (multiTags) {
        const bucket = target.getAttribute("data-pcr-chip-bucket");
        const key = `multi:${multiTags}`;
        if (key === tooltipKey && tooltipEl && tooltipEl.style.display !== "none") {
          positionTooltip(event.clientX, event.clientY);
          return false;
        }
        const idx = getChipIndex();
        const infos = multiTags.split(",")
          .map((t) => findChipByTag(idx, t, bucket))
          .filter(Boolean);
        if (!infos.length) { hideTooltip(); return false; }
        renderCompositeTooltip(infos, event.clientX, event.clientY);
        tooltipKey = key;
        return false;
      }
      const tag = target.getAttribute("data-pcr-chip-tag");
      const bucket = target.getAttribute("data-pcr-chip-bucket");
      const overrideScope = target.getAttribute("data-pcr-chip-override-scope");
      const overrideId = target.getAttribute("data-pcr-chip-override-id");
      const key = `${bucket}:${tag}:${overrideScope || ""}:${overrideId || ""}`;
      if (key === tooltipKey && tooltipEl && tooltipEl.style.display !== "none") {
        positionTooltip(event.clientX, event.clientY);
        return false;
      }
      const info = findChipByTag(getChipIndex(), tag, bucket, overrideScope, overrideId);
      if (!info) { hideTooltip(); return false; }
      renderTooltip(info, event.clientX, event.clientY);
      tooltipKey = key;
      return false;
    },
    mouseleave: () => { hideTooltip(); return false; },
  });

  return [decorationsField, plugin, tooltipHandlers];
}
