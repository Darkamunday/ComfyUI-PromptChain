<script>
  // FantasyCustomizer -- modal for customizing fantasy features with shape, color, type.

  let {
    itemInfo,
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let loading = $state(true);
  let customizerData = $state(null);
  let selectedShape = $state("");
  let selectedColor = $state("");
  let selectedType = $state("");

  // Filter out modifiers already in the feature tag name
  // e.g., "cat_ears" should not show "cat" type, "black_wings" should not show "black" color
  let featureTag = $derived((itemInfo.tag || "").toLowerCase());

  let filteredShapes = $derived(
    (customizerData?.shapes || []).filter(s => !featureTag.includes(s.tag.toLowerCase()))
  );
  let filteredColors = $derived(
    (customizerData?.colors || []).filter(c => !featureTag.includes(c.tag.toLowerCase()))
  );
  let filteredTypes = $derived(
    (customizerData?.types || []).filter(t => !featureTag.includes(t.tag.toLowerCase()))
  );

  let previewText = $derived.by(() => {
    if (!customizerData) return itemInfo.display.toLowerCase();
    const parts = [];
    if (selectedShape) {
      const s = customizerData.shapes.find(x => x.tag === selectedShape);
      if (s?.base_tags) parts.push(s.base_tags);
    }
    if (selectedColor) {
      const c = customizerData.colors.find(x => x.tag === selectedColor);
      if (c?.base_tags) parts.push(c.base_tags);
    }
    if (selectedType) {
      const t = customizerData.types.find(x => x.tag === selectedType);
      if (t?.base_tags) parts.push(t.base_tags);
    }
    parts.push(itemInfo.display.toLowerCase());
    return parts.join(" ");
  });

  $effect(() => {
    fetchData();
  });

  async function fetchData() {
    try {
      const response = await fetch("/promptchain/fantasy/customizer-data");
      if (!response.ok) throw new Error("Failed to fetch fantasy customizer data");
      customizerData = await response.json();
    } catch (e) {
      console.error("[TagBuilder] Failed to load fantasy customizer data:", e);
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

  function handleConfirm() {
    if (!customizerData) return;

    const phrase = previewText;
    const hasCustomization = selectedShape || selectedColor || selectedType;

    const displayParts = [];
    if (selectedShape) {
      const s = customizerData.shapes.find(x => x.tag === selectedShape);
      if (s) displayParts.push(s.display);
    }
    if (selectedColor) {
      const c = customizerData.colors.find(x => x.tag === selectedColor);
      if (c) displayParts.push(c.display);
    }
    if (selectedType) {
      const t = customizerData.types.find(x => x.tag === selectedType);
      if (t) displayParts.push(t.display);
    }
    displayParts.push(itemInfo.display);

    onConfirm({
      tag: itemInfo.tag,
      tags: hasCustomization ? phrase : (itemInfo.tags || phrase),
      natlang: hasCustomization ? phrase : (itemInfo.natlang || phrase),
      display: displayParts.join(" "),
      isCustomized: !!hasCustomization,
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
          <label>Shape:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedShape}>
            <option value="">-- None --</option>
            {#each filteredShapes as s}
              <option value={s.tag}>{s.display}</option>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row">
          <label>Color:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedColor}>
            <option value="">-- None --</option>
            {#each filteredColors as c}
              <option value={c.tag}>{c.display}</option>
            {/each}
          </select>
        </div>

        <div class="pcr-atb-customizer-row">
          <label>Type:</label>
          <select class="pcr-atb-customizer-select" bind:value={selectedType}>
            <option value="">-- None --</option>
            {#each filteredTypes as t}
              <option value={t.tag}>{t.display}</option>
            {/each}
          </select>
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
