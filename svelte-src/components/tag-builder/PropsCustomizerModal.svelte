<script>
  // PropsCustomizerModal -- modal for customizing a prop with material, pattern, color, action.

  let {
    category,
    propsData,
    preSelectedProp = null,
    isNaturalMode = false,
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let selectedProp = $state(preSelectedProp || "");
  let selectedMaterial = $state("");
  let selectedPattern = $state("");
  let selectedColor = $state("");
  let selectedAction = $state("");
  let previewText = $state("Select an item");

  // Category metadata
  let catMeta = $derived(propsData.categories?.find(c => c.category === category));
  let categoryProps = $derived((propsData.props || []).filter(p => p.category === category));

  // Whether the currently selected prop is customizable (furniture-style)
  let isCustomizable = $derived.by(() => {
    if (!selectedProp) return false;
    const prop = categoryProps.find(p => p.prop_tag === selectedProp);
    return prop?.is_customizable || false;
  });

  // Actions filtered by prop overrides or category compatibility
  let availableActions = $derived.by(() => {
    const overrides = propsData.action_overrides || {};
    const propOverrides = selectedProp ? overrides[selectedProp] : null;

    if (propOverrides && propOverrides.length > 0) {
      return propOverrides
        .map(actionTag => propsData.actions?.find(a => a.action_tag === actionTag))
        .filter(Boolean);
    }
    return (propsData.actions || []).filter(a => {
      const compatible = a.compatible_categories;
      return Array.isArray(compatible) && compatible.includes(category);
    });
  });

  // Materials for furniture customization
  let materials = $derived(propsData.materials || []);
  let patterns = $derived((propsData.patterns || []).filter(p => p.tag !== "solid"));

  // Group colors by color_group
  let colorGroups = $derived.by(() => {
    const groups = {};
    for (const c of propsData.colors || []) {
      if (!groups[c.color_group]) groups[c.color_group] = [];
      groups[c.color_group].push(c);
    }
    return Object.entries(groups);
  });

  // Pattern enabled only when material supports it
  let patternEnabled = $derived.by(() => {
    if (!selectedMaterial) return false;
    const mat = materials.find(m => m.tag === selectedMaterial);
    return mat?.supports_patterns === 1 || mat?.supports_patterns === true;
  });

  // Update preview whenever selections change
  $effect(() => {
    // Track all reactive deps
    const _prop = selectedProp;
    const _mat = selectedMaterial;
    const _pat = selectedPattern;
    const _col = selectedColor;
    const _act = selectedAction;
    const _cust = isCustomizable;
    const _natMode = isNaturalMode;

    updatePreview();
  });

  // Reset furniture options when prop changes
  $effect(() => {
    const _prop = selectedProp;
    if (!isCustomizable) {
      selectedMaterial = "";
      selectedPattern = "";
      selectedColor = "";
    }
    // Refresh action list when prop changes
    updateActionDropdown();
  });

  // Disable pattern when material doesn't support it
  $effect(() => {
    if (!patternEnabled) {
      selectedPattern = "";
    }
  });

  function updateActionDropdown() {
    // If the current action is no longer available, clear it
    if (selectedAction && !availableActions.find(a => a.action_tag === selectedAction)) {
      selectedAction = "";
    }
  }

  async function updatePreview() {
    if (!selectedProp) {
      previewText = "Select an item";
      return;
    }

    const body = { prop: selectedProp };
    if (isCustomizable) {
      if (selectedMaterial) body.material = selectedMaterial;
      if (selectedPattern) body.pattern = selectedPattern;
      if (selectedColor) body.color = selectedColor;
    }
    if (selectedAction) body.action = selectedAction;

    try {
      const resp = await fetch("/promptchain/props/assemble", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        const result = await resp.json();
        previewText = isNaturalMode ? result.natlang : result.tags;
      }
    } catch (e) {
      console.error("[Props] Preview error:", e);
    }
  }

  let canConfirm = $derived(!!selectedProp);

  async function handleConfirm() {
    if (!selectedProp) return;

    const body = { prop: selectedProp };
    if (isCustomizable) {
      if (selectedMaterial) body.material = selectedMaterial;
      if (selectedPattern) body.pattern = selectedPattern;
      if (selectedColor) body.color = selectedColor;
    }
    if (selectedAction) body.action = selectedAction;

    try {
      const resp = await fetch("/promptchain/props/assemble", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        const result = await resp.json();

        const displayParts = [];
        if (result.parts.action) displayParts.push(result.parts.action.display_name);
        if (result.parts.color) displayParts.push(result.parts.color.display);
        if (result.parts.pattern) displayParts.push(result.parts.pattern.display);
        if (result.parts.material) displayParts.push(result.parts.material.display);
        displayParts.push(result.parts.prop.display_name);

        onConfirm({
          prop: selectedProp,
          category,
          material: body.material || null,
          pattern: body.pattern || null,
          color: body.color || null,
          action: body.action || null,
          tags: result.tags,
          natlang: result.natlang,
          display: displayParts.join(" "),
        });
      }
    } catch (e) {
      console.error("[Props] Add error:", e);
    }
  }

  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) onCancel();
  }

  function handleKeydown(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onCancel();
    } else if (e.key === "Enter" && canConfirm) {
      e.stopPropagation();
      handleConfirm();
    }
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="pcr-atb-customizer-overlay" onclick={handleOverlayClick} onkeydown={handleKeydown}>
  <div class="pcr-atb-customizer-modal">
    <div class="pcr-atb-customizer-header">
      <span class="pcr-atb-customizer-title">
        {catMeta?.icon || ""} {catMeta?.display_name || category}
      </span>
      <button class="pcr-atb-customizer-close" onclick={onCancel}>&times;</button>
    </div>

    <div class="pcr-atb-customizer-body">
      <div class="pcr-atb-customizer-row">
        <label>Item:</label>
        <select class="pcr-atb-customizer-select" bind:value={selectedProp}>
          <option value="">-- Select item --</option>
          {#each categoryProps as p}
            <option value={p.prop_tag}>
              {p.display_name}{p.is_customizable ? " \u2605" : ""}
            </option>
          {/each}
        </select>
      </div>

      {#if isCustomizable}
        <div class="pcr-atb-customizer-furniture-opts" style="display: flex; flex-direction: column;">
          <div class="pcr-atb-customizer-row">
            <label>Material:</label>
            <select class="pcr-atb-customizer-select" bind:value={selectedMaterial}>
              <option value="">-- None --</option>
              {#each materials as m}
                <option value={m.tag}>{m.display}</option>
              {/each}
            </select>
          </div>

          <div class="pcr-atb-customizer-row">
            <label>Pattern:</label>
            <select class="pcr-atb-customizer-select" bind:value={selectedPattern} disabled={!patternEnabled}>
              <option value="">-- Solid --</option>
              {#each patterns as p}
                <option value={p.tag}>{p.display}</option>
              {/each}
            </select>
          </div>

          <div class="pcr-atb-customizer-row">
            <label>Color:</label>
            <select class="pcr-atb-customizer-select" bind:value={selectedColor}>
              <option value="">-- None --</option>
              {#each colorGroups as [group, colors]}
                <optgroup label={group.charAt(0).toUpperCase() + group.slice(1)}>
                  {#each colors as c}
                    <option value={c.tag}>{c.display}</option>
                  {/each}
                </optgroup>
              {/each}
            </select>
          </div>
        </div>
      {/if}

      <div class="pcr-atb-customizer-row">
        <label>Action:</label>
        <select class="pcr-atb-customizer-select" bind:value={selectedAction}>
          <option value="">-- No action --</option>
          {#each availableActions as a}
            <option value={a.action_tag}>{a.display_name}</option>
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
      <button class="pcr-atb-customizer-btn pcr-atb-customizer-ok" disabled={!canConfirm} onclick={handleConfirm}>Add Prop</button>
    </div>
  </div>
</div>
