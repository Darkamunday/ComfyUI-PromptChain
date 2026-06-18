// Shared chip lookup index. Loaded once per session (lazy) from the
// tag-builder bucket endpoints, then reused by anything that needs to
// recognize chip natlangs in arbitrary text (e.g. the CodeMirror
// chip-recognizer extension). Mirrors the in-memory shape that
// TagBuilder2 builds on its own — extracted here so consumers outside
// the Svelte component can share it without duplicating the load.

const BUCKETS = ["clothing", "appearance", "pose", "expression", "action", "scene", "nsfw_action", "cast"];

let cachedIndex = null;
let loadPromise = null;

// Event listeners notified when the index finishes loading or refreshes.
const listeners = new Set();

function notifyReady() {
  for (const fn of listeners) {
    try { fn(cachedIndex); } catch {}
  }
}

export function onChipIndexReady(fn) {
  listeners.add(fn);
  if (cachedIndex) fn(cachedIndex);
  return () => listeners.delete(fn);
}

export function getChipIndex() {
  return cachedIndex;
}

// Forces a fresh load. Used after a character/chip mutation so consumers
// (tooltips, recognizers) pick up the new data on the next hover without
// a page reload. Resets the cache and re-runs loadChipIndex.
export async function reloadChipIndex() {
  cachedIndex = null;
  loadPromise = null;
  return loadChipIndex();
}

// Fetches every bucket's items and builds a lookup index. First call
// kicks off the network load; subsequent calls share the same promise.
// On success, cachedIndex is populated and onChipIndexReady listeners
// fire.
//
// Characters are loaded too but only the normalized slice — the full
// table is 11k rows and we only need verified character base_natlangs
// in the recognizer. Matches the filter v2 uses for its character
// drilldown.
export function loadChipIndex() {
  if (cachedIndex) return Promise.resolve(cachedIndex);
  if (loadPromise) return loadPromise;
  loadPromise = (async () => {
    const buckets = {};
    const bucketLoads = BUCKETS.map(async (b) => {
      try {
        const res = await fetch(`/promptchain/tag-builder/buckets/${b}/items`);
        if (!res.ok) { buckets[b] = []; return; }
        const data = await res.json();
        buckets[b] = data.items || [];
      } catch {
        buckets[b] = [];
      }
    });
    const charactersLoad = (async () => {
      try {
        const res = await fetch("/promptchain/tag-builder/characters?natlang_status=normalized&per_page=2000");
        if (!res.ok) return [];
        const data = await res.json();
        return data.results || data.characters || [];
      } catch {
        return [];
      }
    })();
    const overridesLoad = async (url, scope) => {
      try {
        const res = await fetch(url);
        if (!res.ok) return [];
        const data = await res.json();
        return (data.overrides || []).map(o => ({ ...o, scope }));
      } catch {
        return [];
      }
    };
    const charOvLoad = overridesLoad("/promptchain/tag-builder/character-overrides", "character");
    const outfitOvLoad = overridesLoad("/promptchain/tag-builder/outfit-overrides", "outfit");
    const poseOvLoad = overridesLoad("/promptchain/tag-builder/pose-overrides", "pose");
    const [characters, charOvs, outfitOvs, poseOvs] = await Promise.all([
      charactersLoad, charOvLoad, outfitOvLoad, poseOvLoad, ...bucketLoads,
    ]);
    cachedIndex = buildIndex(buckets, characters, [...charOvs, ...outfitOvs, ...poseOvs]);
    loadPromise = null;
    notifyReady();
    return cachedIndex;
  })();
  return loadPromise;
}

function buildIndex(bucketItems, characters, overrides = []) {
  // phraseToChip: natlang phrase → chip info. First-write-wins across
  // buckets so a clothing chip beats a scene chip on the same phrase.
  const phraseToChip = new Map();
  // multiCommaList: chips whose natlang itself contains commas (e.g. an
  // armor chip whose base_natlang is a 6-piece description). Sorted by
  // length DESC for longest-first matching in the scanner.
  const multiCommaList = [];
  // characterList: normalized characters with a non-empty base_natlang,
  // sorted by length DESC for longest-first matching. Characters share
  // the scanner's match-and-claim pass but get their own bucket label
  // ("characters") so the thumbnail URL and tooltip render correctly.
  const characterList = [];
  // overrideList: per-character chip enhancer phrasings. The recognizer
  // walks this list first so an override like Cammy's verbose scar text
  // claims its span before generic chip natlangs get a chance to match
  // anything overlapping.
  const overrideList = [];

  // Cross-bucket tag → info map, used when resolving override chip refs
  // (the source chip can live in any bucket) and when looking up a
  // character's appearance_chip_tags.
  const chipByTag = new Map();

  // tagToChip: tag form → chip info, used as a recognizer fallback so
  // tag-mode output resolves even when it differs from base_natlang.
  // Keyed by both the underscore item_tag ("double_bun") and its
  // spaces variant ("double bun") so it matches whichever tag_format
  // the model emits. First-write-wins across buckets.
  const tagToChip = new Map();

  // Hair-weld decomposition support. The server's
  // compose_character_natlang_v2 binds a hair_length adjective onto the
  // primary hair_style phrase ("long" + "twin braids" -> "long twin
  // braids") so a single natlang feature stands in for two canonical
  // tags. The recognizer reverses that to keep both chips hoverable:
  //   hairStyleByPhrase: "twin braids" -> twin_braids info (suffix match)
  //   hairLengthAdj:     "long"        -> long_hair info  (prefix match)
  // hairLengthAdj keys are the bare adjective (base_natlang minus the
  // trailing "hair"/"hairs"), mirroring server _hair_length_adjective.
  const hairStyleByPhrase = new Map();
  const hairLengthAdj = new Map();

  for (const bucket of BUCKETS) {
    const items = bucketItems[bucket] || [];
    for (const it of items) {
      const nl = (it.base_natlang || "").trim().replace(/\.\s*$/, "");
      const info = {
        item_tag: it.item_tag,
        display_name: it.display_name || it.item_tag,
        item_group: it.item_group || "",
        base_tags: it.base_tags || "",
        base_natlang: nl,
        natlang_status: it.natlang_status || "unprocessed",
        bucket,
      };
      chipByTag.set(`${bucket}:${it.item_tag}`, info);
      // Key the tag fallback by both the item_tag AND the emitted base_tags
      // token (they differ for prefixed rows: item_tag
      // "hair_style_side_drill" but base_tags "side_drill" — and the prompt
      // emits base_tags). Register underscore + space forms of each so it
      // resolves under either tag_format.
      const tagKeys = new Set();
      const addKey = (raw) => {
        const k = (raw || "").trim().toLowerCase();
        if (!k) return;
        tagKeys.add(k);
        tagKeys.add(k.replace(/_/g, " "));
      };
      addKey(it.item_tag);
      // base_tags is the emitted form; only use it when it's a single clean
      // token (no commas, no weight wrapper) so we don't key on a phrase.
      const bt = (it.base_tags || "").trim();
      if (bt && !bt.includes("(")) {
        if (!bt.includes(",")) {
          addKey(bt);
        } else if (bucket === "cast") {
          // Cast identity rows carry a multi-tag base_tags whose FIRST token is
          // the emitted subject tag (e.g. "male_maid, otoko_no_ko, 1boy" emits
          // "male maid"). Key that leading token so the subject chip is
          // hoverable; item_tag itself is prefixed ("archetype_male_maid") and
          // never appears in the prompt.
          addKey(bt.split(",")[0]);
        }
      }
      for (const k of tagKeys) if (!tagToChip.has(k)) tagToChip.set(k, info);
      if (bucket === "appearance" && nl) {
        const lower = nl.toLowerCase();
        if (info.item_group === "hair_style" && !hairStyleByPhrase.has(lower)) {
          hairStyleByPhrase.set(lower, info);
        } else if (info.item_group === "hair_length") {
          const words = lower.split(/\s+/);
          if (words.length >= 2 && (words[words.length - 1] === "hair" || words[words.length - 1] === "hairs")) {
            const adj = words.slice(0, -1).join(" ");
            if (!hairLengthAdj.has(adj)) hairLengthAdj.set(adj, info);
          }
        }
      }
      if (!nl) continue;
      if (nl.includes(",")) {
        multiCommaList.push({ natlang: nl, info });
      } else if (!phraseToChip.has(nl)) {
        phraseToChip.set(nl, info);
      }
    }
  }
  multiCommaList.sort((a, b) => b.natlang.length - a.natlang.length);

  // characterByTag: needed so override entries can carry the character's
  // display name into their tooltip's "Enhancer for: Cammy White" sub-row.
  const characterByTag = new Map();

  for (const c of characters || []) {
    const nl = (c.base_natlang || "").trim().replace(/\.\s*$/, "");
    if (!nl) continue;
    // Resolve appearance chips: if `appearance_chip_tags` is a JSON array,
    // attach the full appearance chip info for each token. Falls back to
    // empty so legacy unmigrated characters just render the flat-string
    // tooltip we always had.
    let appearanceChips = [];
    if (c.appearance_chip_tags) {
      try {
        const tags = JSON.parse(c.appearance_chip_tags) || [];
        for (const t of tags) {
          const chipInfo = chipByTag.get(`appearance:${t}`);
          if (chipInfo) appearanceChips.push(chipInfo);
        }
      } catch {}
    }
    const characterInfo = {
      item_tag: c.tag,
      display_name: c.display || c.tag,
      item_group: c.series || "",
      base_tags: c.base_tags || "",
      base_natlang: nl,
      natlang_status: c.natlang_status || "unprocessed",
      bucket: "characters",
      identity_token: `(${c.tag}:1.1)`,
      base_extras: c.base_extras || "",
      appearance_chips: appearanceChips,
      migrated: !!(c.appearance_chip_tags),
    };
    characterByTag.set(c.tag, characterInfo);
    characterList.push({ natlang: nl, info: characterInfo });
  }
  characterList.sort((a, b) => b.natlang.length - a.natlang.length);

  // Overrides: each row is an enhancer phrasing pinned to either a
  // character, outfit, or pose. The chip can live in any bucket; we
  // resolve by walking every bucket's tag map. If the source chip is
  // gone (deleted bucket row), we still surface the override but with
  // sourceChip null so the tooltip degrades gracefully.
  //
  // Scope shape — for each entry:
  //   scope: "character" | "outfit" | "pose"
  //   scope_id: character_tag | outfit_id | pose_id
  //   scope_display: human-readable label ("Cammy White", "Delta Red", ...)
  //   scope_character_display: owning character (outfit/pose only) so
  //     the tooltip can show "Delta Red (Cammy White)" instead of just
  //     the outfit name which would lose context.
  for (const o of overrides) {
    const nl = (o.natlang || "").trim().replace(/\.\s*$/, "");
    if (!nl) continue;
    let sourceChip = null;
    for (const bucket of BUCKETS) {
      const c = chipByTag.get(`${bucket}:${o.chip_tag}`);
      if (c) { sourceChip = c; break; }
    }
    let scopeId, scopeDisplay, scopeCharacterDisplay;
    if (o.scope === "character") {
      scopeId = o.character_tag;
      scopeDisplay = o.character_display || o.character_tag;
      scopeCharacterDisplay = null;
    } else if (o.scope === "outfit") {
      scopeId = o.outfit_id;
      scopeDisplay = o.outfit_name || `Outfit ${o.outfit_id}`;
      scopeCharacterDisplay = o.character_display || o.character_tag;
    } else if (o.scope === "pose") {
      scopeId = o.pose_id;
      scopeDisplay = o.pose_name || `Pose ${o.pose_id}`;
      scopeCharacterDisplay = o.character_display || o.character_tag;
    }
    overrideList.push({
      natlang: nl,
      info: {
        // Override entries point back at the source chip's bucket so
        // tooltips and thumbnails route to the right place, but carry
        // override metadata in dedicated fields so the tooltip can
        // render the Enhancer section.
        item_tag: o.chip_tag,
        display_name: sourceChip?.display_name || o.chip_tag,
        item_group: sourceChip?.item_group || "",
        base_tags: sourceChip?.base_tags || "",
        base_natlang: sourceChip?.base_natlang || "",
        natlang_status: sourceChip?.natlang_status || "unprocessed",
        bucket: sourceChip?.bucket || "appearance",
        override_scope: o.scope,
        override_scope_id: scopeId,
        override_scope_display: scopeDisplay,
        override_scope_character_display: scopeCharacterDisplay,
        override_natlang: nl,
        override_status: o.status || "unprocessed",
      },
    });
  }
  overrideList.sort((a, b) => b.natlang.length - a.natlang.length);

  return { phraseToChip, tagToChip, multiCommaList, characterList, overrideList, hairStyleByPhrase, hairLengthAdj };
}
