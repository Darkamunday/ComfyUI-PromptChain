function stripLeadingSectionHeader(text) {
  if (!text) return text;
  return text.replace(/^\s*\/\/\s*[A-Za-z][^\n]*\n+/, "");
}
function escapeTagParens(tags) {
  if (!tags) return tags;
  let result = "";
  let i = 0;
  while (i < tags.length) {
    if (tags[i] === "(") {
      let depth = 1;
      let j = i + 1;
      let colonPos = -1;
      while (j < tags.length && depth > 0) {
        if (tags[j] === "(") depth++;
        else if (tags[j] === ")") depth--;
        else if (tags[j] === ":" && depth === 1) colonPos = j;
        j++;
      }
      if (colonPos > 0 && depth === 0) {
        const afterColon = tags.slice(colonPos + 1, j - 1);
        if (/^\d+\.?\d*$/.test(afterColon)) {
          const content = tags.slice(i + 1, colonPos);
          const weight = afterColon;
          const escapedContent = content.replace(/\(/g, "\\(").replace(/\)/g, "\\)");
          result += `(${escapedContent}:${weight})`;
          i = j;
          continue;
        }
      }
      result += "\\(";
      i++;
      continue;
    }
    if (tags[i] === ")") {
      result += "\\)";
      i++;
      continue;
    }
    result += tags[i];
    i++;
  }
  return result;
}
function formatTagsForModel(tags, tagSourceConfig) {
  if (!tags) return "";
  if ((tagSourceConfig == null ? void 0 : tagSourceConfig.format) === "spaces") {
    return tags.split(",").map((tag) => tag.trim().replace(/_/g, " ")).join(", ");
  }
  return tags;
}
function buildInsertText(state, isAtLineStart, tagSourceConfig) {
  {
    return state.isNaturalMode ? buildStructuredNaturalLanguage(state) : buildStructuredTags(state, tagSourceConfig);
  }
}
function buildStructuredNaturalLanguage(state) {
  var _a, _b, _c, _d, _e;
  const sections = [];
  const characters = state.selections.characters || [];
  for (const char of characters) {
    const charParts = [];
    const cachedChar = char.tag ? (_a = state.cache) == null ? void 0 : _a[`char:${char.tag}`] : null;
    const series = (cachedChar == null ? void 0 : cachedChar.series) || "";
    let charName = char.display;
    if (series && charName.endsWith(`(${series})`)) {
      charName = charName.slice(0, -(series.length + 3)).trim();
    }
    let header = series ? `// Character: ${charName} (${series})` : `// Character: ${charName}`;
    if (char.base && char.baseNatlang) {
      let natlang = stripLeadingSectionHeader(char.baseNatlang);
      if ((_b = char.outfit) == null ? void 0 : _b.overridesNatlang) {
        const toRemove = char.outfit.overridesNatlang.split("|").map((p) => p.trim()).filter(Boolean);
        for (const phrase of toRemove) natlang = natlang.replace(phrase, "");
        natlang = natlang.replace(/\s+/g, " ").trim();
      }
      charParts.push(natlang);
    }
    if (!char.outfit && ((_c = char.pose) == null ? void 0 : _c.natlang)) charParts.push(stripLeadingSectionHeader(char.pose.natlang));
    if ((_d = char.outfit) == null ? void 0 : _d.natlang) {
      if (charParts.length > 0) {
        let charText = charParts.join(" ");
        if (!charText.endsWith(".")) charText += ".";
        sections.push(header + "\n" + charText);
        charParts.length = 0;
      }
      const outfitHeader = `// Outfit: ${char.outfit.display} from Character: ${charName}`;
      const outfitBody = stripLeadingSectionHeader(char.outfit.natlang);
      sections.push(outfitHeader + "\n" + (outfitBody.endsWith(".") ? outfitBody : outfitBody + "."));
    } else if (charParts.length > 0) {
      let charText = charParts.join(" ");
      if (!charText.endsWith(".")) charText += ".";
      sections.push(header + "\n" + charText);
    }
    if (char.outfit && ((_e = char.pose) == null ? void 0 : _e.natlang)) {
      const poseHeader = `// Pose: ${char.pose.display} from Character: ${charName}`;
      const poseBody = stripLeadingSectionHeader(char.pose.natlang);
      sections.push(poseHeader + "\n" + (poseBody.endsWith(".") ? poseBody : poseBody + "."));
    }
  }
  const clothingItems = [];
  if (state.selections.clothing) {
    for (const groupName in state.selections.clothing) {
      const sel = state.selections.clothing[groupName];
      const items = Array.isArray(sel) ? sel : [sel];
      for (const item of items) {
        if (item.natlang) clothingItems.push(item.natlang);
        else if (item.tags) clothingItems.push(item.tags);
      }
    }
  }
  if (clothingItems.length > 0) {
    let clothingText = "Wearing " + clothingItems.join(", ");
    if (!clothingText.endsWith(".")) clothingText += ".";
    sections.push("// Clothing\n" + clothingText);
  }
  const otherParts = [];
  for (const bucket of ["cast", "appearance", "pose", "scene", "expression", "action", "nsfw_action"]) {
    const selections = state.selections[bucket];
    if (!selections) continue;
    for (const groupName in selections) {
      const sel = selections[groupName];
      const items = Array.isArray(sel) ? sel : [sel];
      for (const item of items) {
        if (item.natlang) otherParts.push(item.natlang);
        else if (item.tags) otherParts.push(item.tags);
      }
    }
  }
  if (otherParts.length > 0) {
    let otherText = otherParts.join(" ");
    if (!otherText.endsWith(".")) otherText += ".";
    sections.push("// Additional Details\n" + otherText);
  }
  const props = state.selections.props || [];
  if (props.length > 0) {
    const propTexts = props.map((p) => {
      let text = p.natlang || p.tags;
      if (text && !text.endsWith(".")) text += ".";
      return text;
    }).filter(Boolean);
    if (propTexts.length > 0) sections.push("// Props\n" + propTexts.join(" "));
  }
  return sections.join("\n\n");
}
function buildStructuredTags(state, tagSourceConfig) {
  var _a, _b, _c, _d, _e;
  const sections = [];
  const characters = state.selections.characters || [];
  for (const char of characters) {
    const charParts = [];
    const cachedChar = char.tag ? (_a = state.cache) == null ? void 0 : _a[`char:${char.tag}`] : null;
    const series = (cachedChar == null ? void 0 : cachedChar.series) || "";
    let charName = char.display;
    let header = series ? `// Character: ${charName} (${series})` : `// Character: ${charName}`;
    if (char.base && char.baseTags) {
      let tags = char.baseTags;
      if ((_b = char.outfit) == null ? void 0 : _b.overridesTags) {
        const toRemove = char.outfit.overridesTags.split(",").map((t) => t.trim()).filter(Boolean);
        for (const tag of toRemove) {
          const escapedTag = tag.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          tags = tags.replace(new RegExp(`\\(${escapedTag}:[^)]+\\),?\\s*`, "gi"), "");
          tags = tags.replace(new RegExp(`${escapedTag},?\\s*`, "gi"), "");
        }
        tags = tags.replace(/,\s*$/, "").replace(/,\s*,/g, ",");
      }
      charParts.push(escapeTagParens(tags));
    }
    if (!char.outfit && ((_c = char.pose) == null ? void 0 : _c.tags)) charParts.push(escapeTagParens(char.pose.tags));
    if ((_d = char.outfit) == null ? void 0 : _d.tags) {
      if (charParts.length > 0) {
        sections.push(header + "\n" + charParts.join(", "));
        charParts.length = 0;
      }
      const outfitHeader = `// Outfit: ${char.outfit.display} from Character: ${charName}`;
      sections.push(outfitHeader + "\n" + escapeTagParens(char.outfit.tags));
    } else if (charParts.length > 0) {
      sections.push(header + "\n" + charParts.join(", "));
    }
    if (char.outfit && ((_e = char.pose) == null ? void 0 : _e.tags)) {
      const poseHeader = `// Pose: ${char.pose.display} from Character: ${charName}`;
      sections.push(poseHeader + "\n" + escapeTagParens(char.pose.tags));
    }
  }
  const clothingItems = [];
  if (state.selections.clothing) {
    for (const groupName in state.selections.clothing) {
      const sel = state.selections.clothing[groupName];
      const items = Array.isArray(sel) ? sel : [sel];
      for (const item of items) if (item.tags) clothingItems.push(escapeTagParens(item.tags));
    }
  }
  if (clothingItems.length > 0) sections.push("// Clothing\n" + clothingItems.join(", "));
  const otherParts = [];
  for (const bucket of ["cast", "appearance", "pose", "scene", "expression", "action", "nsfw_action"]) {
    const selections = state.selections[bucket];
    if (!selections) continue;
    for (const groupName in selections) {
      const sel = selections[groupName];
      const items = Array.isArray(sel) ? sel : [sel];
      for (const item of items) if (item.tags) otherParts.push(escapeTagParens(item.tags));
    }
  }
  if (otherParts.length > 0) sections.push("// Additional Details\n" + otherParts.join(", "));
  const props = state.selections.props || [];
  if (props.length > 0) {
    const propTags = props.map((p) => escapeTagParens(p.tags)).filter(Boolean);
    if (propTags.length > 0) sections.push("// Props\n" + propTags.join(", "));
  }
  return sections.map((section) => {
    const lines = section.split("\n");
    if (lines.length === 2) return lines[0] + "\n" + formatTagsForModel(lines[1], tagSourceConfig);
    return section;
  }).join("\n\n");
}
export {
  buildInsertText as b,
  formatTagsForModel as f
};
//# sourceMappingURL=tag-builder-utils-ng134QDV.js.map
