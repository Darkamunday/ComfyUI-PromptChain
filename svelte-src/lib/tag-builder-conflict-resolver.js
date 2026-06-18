// Conflict resolver: when user-added clothing items occupy the same slot
// as phrases baked into a character's selected outfit, strip the outfit
// phrases and route them into negatives so the model is pushed off the
// character's trained-in defaults.
//
// Inputs:
//   outfitSlots:        array of { slot, item, color, source_phrase }
//   userClothingSelections: object keyed by slot name (state.selections.clothing)
//
// Output:
//   { stripPhrases: Set<string>, negatives: string[] }
//
// Slots like 'accessories' and 'modifiers' are additive — never auto-stripped
// since the outfit may legitimately wear multiple of them alongside user picks.

const ADDITIVE_SLOTS = new Set(["accessories", "modifiers"]);

export function resolveOutfitConflicts(outfitSlots, userClothingSelections) {
  const stripPhrases = new Set();
  const negatives = [];

  if (!outfitSlots?.length || !userClothingSelections) {
    return { stripPhrases, negatives };
  }

  const occupiedByUser = new Set();
  for (const groupName in userClothingSelections) {
    const sel = userClothingSelections[groupName];
    const items = Array.isArray(sel) ? sel : [sel];
    if (items.some(it => it && (it.tag || it.tags))) {
      occupiedByUser.add(groupName.toLowerCase());
    }
  }

  for (const row of outfitSlots) {
    const slot = (row.slot || "").toLowerCase();
    if (!slot || ADDITIVE_SLOTS.has(slot)) continue;
    if (!occupiedByUser.has(slot)) continue;
    if (!row.source_phrase) continue;
    stripPhrases.add(row.source_phrase);
    negatives.push(row.source_phrase);
  }

  return { stripPhrases, negatives };
}

// Strip a set of comma-separated tag phrases from a tag blob without
// breaking surrounding tags. Handles weighted forms like (tag:1.1) too.
export function stripPhrasesFromTagBlob(tags, stripPhrases) {
  if (!tags || !stripPhrases?.size) return tags;
  let result = tags;
  for (const phrase of stripPhrases) {
    const escaped = phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    result = result.replace(new RegExp(`\\(${escaped}:[^)]+\\),?\\s*`, "gi"), "");
    result = result.replace(new RegExp(`(^|,\\s*)${escaped}(?=,|$)`, "gi"), (m, sep) => sep ? "" : "");
  }
  return result.replace(/,\s*,/g, ",").replace(/^\s*,\s*/, "").replace(/,\s*$/, "").trim();
}
