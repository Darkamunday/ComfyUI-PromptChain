<script>
  // ClothingCustomizer -- modal for customizing clothing with color, pattern, material, condition, focus.

  let {
    itemInfo,
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let loading = $state(true);
  let customizerData = $state(null);
  let selectedColor = $state("");
  let selectedPattern = $state("solid");
  let selectedMaterial = $state("");
  let selectedCondition = $state("default");
  let hasFocus = $state(false);

  // Organized data structures derived from raw API response
  let sortedColorGroups = $derived.by(() => {
    if (!customizerData?.colors) return [];
    const groups = {};
    for (const c of customizerData.colors) {
      if (!groups[c.color_group]) groups[c.color_group] = [];
      groups[c.color_group].push(c);
    }
    // Sort items within each group alphabetically
    for (const colors of Object.values(groups)) {
      colors.sort((a, b) => a.display.localeCompare(b.display));
    }
    // "neutral" first, then alphabetically
    return Object.entries(groups).sort(([a], [b]) => {
      if (a.toLowerCase() === "neutral") return -1;
      if (b.toLowerCase() === "neutral") return 1;
      return a.localeCompare(b);
    });
  });

  let patternGroups = $derived.by(() => {
    if (!customizerData?.patterns) return {};
    const groups = {};
    for (const p of customizerData.patterns) {
      if (!groups[p.pattern_group]) groups[p.pattern_group] = [];
      groups[p.pattern_group].push(p);
    }
    return groups;
  });

  let conditionGroups = $derived.by(() => {
    if (!customizerData?.conditions) return {};
    const groups = {};
    for (const c of customizerData.conditions) {
      if (!groups[c.condition_group]) groups[c.condition_group] = [];
      groups[c.condition_group].push(c);
    }
    return groups;
  });

  let previewText = $derived.by(() => {
    if (!customizerData) return itemInfo.display.toLowerCase();
    return buildPhrase();
  });

  $effect(() => {
    fetchCustomizerData(itemInfo.group);
  });

  async function fetchCustomizerData(group) {
    try {
      const response = await fetch(`/promptchain/clothing/customizer-data?group=${encodeURIComponent(group)}`);
      if (!response.ok) throw new Error("Failed to fetch customizer data");
      customizerData = await response.json();
    } catch (e) {
      console.error("[TagBuilder] Failed to load clothing customizer data:", e);
      // Fall back to basic selection
      onConfirm({
        tag: itemInfo.tag,
        tags: itemInfo.tags || itemInfo.tag,
        natlang: itemInfo.natlang,
        display: itemInfo.display,
        isCustomized: false,
      });
      return;
    }
    loading = false;
  }

  function getOptionPrefix(selectValue, options) {
    if (!selectValue || !options) return "";
    const opt = options.find(o => o.tag === selectValue);
    return opt?.prefix || "";
  }

  function buildPhrase() {
    const parts = [];

    // Color
    const colorPrefix = getOptionPrefix(selectedColor, customizerData?.colors);
    if (colorPrefix) parts.push(colorPrefix);

    // Pattern (skip "solid" which has empty prefix)
    const patternPrefix = getOptionPrefix(selectedPattern, customizerData?.patterns);
    if (patternPrefix) parts.push(patternPrefix);

    // Condition (skip "default" which has empty prefix)
    const conditionPrefix = getOptionPrefix(selectedCondition, customizerData?.conditions);
    if (conditionPrefix) parts.push(conditionPrefix);

    // Material
    const materialPrefix = getOptionPrefix(selectedMaterial, customizerData?.materials);
    if (materialPrefix) parts.push(materialPrefix);

    parts.push(itemInfo.display.toLowerCase());

    const phrase = parts.join(" ");
    if (hasFocus) {
      return `${phrase}, presenting ${phrase} to viewer, ${phrase} focus`;
    }
    return phrase;
  }

  function handleConfirm() {
    const phrase = buildPhrase();
    const hasCustomization = hasFocus ||
      selectedColor !== "" ||
      (selectedPattern !== "" && selectedPattern !== "solid") ||
      selectedMaterial !== "" ||
      (selectedCondition !== "" && selectedCondition !== "default");

    // Build display name
    const displayParts = [];
    if (selectedCondition && selectedCondition !== "default" && customizerData?.conditions) {
      const c = customizerData.conditions.find(x => x.tag === selectedCondition);
      if (c) displayParts.push(c.display);
    }
    if (selectedColor && customizerData?.colors) {
      const c = customizerData.colors.find(x => x.tag === selectedColor);
      if (c) displayParts.push(c.display);
    }
    if (selectedPattern && selectedPattern !== "solid" && customizerData?.patterns) {
      const p = customizerData.patterns.find(x => x.tag === selectedPattern);
      if (p) displayParts.push(p.display);
    }
    if (selectedMaterial && customizerData?.materials) {
      const m = customizerData.materials.find(x => x.tag === selectedMaterial);
      if (m) displayParts.push(m.display);
    }
    displayParts.push(itemInfo.display);
    if (hasFocus) displayParts.push("(focus)");

    onConfirm({
      tag: itemInfo.tag,
      tags: hasCustomization ? phrase : (itemInfo.tags || phrase),
      natlang: hasCustomization ? phrase : (itemInfo.natlang || phrase),
      display: displayParts.join(" "),
      isCustomized: hasCustomization,
    });
  }

  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) onCancel();
  }

  function handleKeydown(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCancel();
    } else if (e.key === "Enter") {
      e.stopPropagation();
      handleConfirm();
    }
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="pcr-atb-customizer-overlay" onclick={handleOverlayClick} onkeydown={handleKeydown}>
  <div class="pcr-atb-customizer-modal">
    <div class="pcr-atb-customizer-header">
      <span class="pcr-atb-customizer-title">Customize: {itemInfo.display}</span>
      <button class="pcr-atb-customizer-close" onclick={onCancel}>&times;</button>
    </div>

    {#if loading}
      <div class="pcr-atb-loading">Loading customizer...</div>
    {:else}
      <div class="pcr-atb-customizer-body">
        <div class="pcr-atb-customizer-row">
          <label>Color:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedColor}>
            <option value="">-- None --</option>
            {#each sortedColorGroups as [group, colors]}
              <optgroup label={group.charAt(0).toUpperCase() + group.slice(1)}>
                {#each colors as c}
                  <option value={c.tag}>{c.display}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row">
          <label>Pattern:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedPattern}>
            {#each Object.entries(patternGroups) as [group, patterns]}
              <optgroup label={group.charAt(0).toUpperCase() + group.slice(1)}>
                {#each patterns as p}
                  <option value={p.tag}>{p.display}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row">
          <label>Material:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedMaterial} disabled={!customizerData?.materials?.length}>
            <option value="">-- None --</option>
            {#each customizerData?.materials || [] as m}
              <option value={m.tag}>{m.display}</option>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row">
          <label>Condition:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedCondition}>
            {#each Object.entries(conditionGroups) as [group, conditions]}
              <optgroup label={group.charAt(0).toUpperCase() + group.slice(1)}>
                {#each conditions as c}
                  <option value={c.tag}>{c.display}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row pcr-atb-customizer-focus-row">
          <label>
            <input type="checkbox" bind:checked={hasFocus}>
            Add focus tags
          </label>
          <span class="pcr-atb-customizer-focus-hint">Camera focuses on this item</span>
        </div>

        <div class="pcr-atb-customizer-preview">
          <div class="pcr-atb-customizer-preview-label">Preview:</div>
          <div class="pcr-atb-customizer-preview-text">{previewText}</div>
        </div>
      </div>

      <div class="pcr-atb-customizer-footer">
        <button class="pcr-atb-customizer-btn pcr-atb-customizer-cancel" onclick={onCancel}>Cancel</button>
        <button class="pcr-atb-customizer-btn pcr-atb-customizer-ok" onclick={handleConfirm}>OK</button>
      </div>
    {/if}
  </div>
</div>
