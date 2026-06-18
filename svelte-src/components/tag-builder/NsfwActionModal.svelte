<script>
  // NsfwActionModal -- tabbed modal for selecting adult action items across multiple groups.

  let {
    groups = [],
    currentSelections = {},
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let activeGroup = $state(groups[0]?.name || "");
  // structuredClone handles Date/Map/Set and avoids the JSON round-trip
  // exception that would kill this modal if currentSelections ever
  // contained a non-serializable value.
  let pendingSelections = $state((() => {
    try { return structuredClone(currentSelections); }
    catch (e) { console.error("[PromptChain] NsfwActionModal clone failed:", e); return {}; }
  })());

  let selectedPills = $derived(Object.values(pendingSelections).filter(s => s?.display));

  function switchGroup(groupName) {
    activeGroup = groupName;
  }

  function selectItem(groupName, item) {
    pendingSelections[groupName] = {
      tag: item.tag,
      tags: item.tags,
      natlang: item.natlang,
      display: item.display,
    };
    // Force reactivity by reassignment
    pendingSelections = { ...pendingSelections };
  }

  function clearGroup(groupName) {
    delete pendingSelections[groupName];
    pendingSelections = { ...pendingSelections };
  }

  function hasGroupSelection(groupName) {
    return !!pendingSelections[groupName];
  }

  function handleConfirm() {
    onConfirm(pendingSelections);
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
  <div class="pcr-atb-customizer-modal pcr-atb-nsfw-modal">
    <div class="pcr-atb-customizer-header">
      <span class="pcr-atb-customizer-title">Adult Actions</span>
      <button class="pcr-atb-customizer-close" onclick={onCancel}>&times;</button>
    </div>

    <div class="pcr-atb-nsfw-tabs">
      {#each groups as group}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="pcr-atb-nsfw-tab"
          class:active={activeGroup === group.name}
          onclick={() => switchGroup(group.name)}
        >
          {group.display}
          {#if hasGroupSelection(group.name)}
            <span class="pcr-atb-nsfw-tab-dot">&#9679;</span>
          {/if}
        </div>
      {/each}
    </div>

    <div class="pcr-atb-nsfw-panels">
      {#each groups as group}
        <div class="pcr-atb-nsfw-panel" class:active={activeGroup === group.name}>
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div class="pcr-atb-nsfw-clear" onclick={() => clearGroup(group.name)}>
            -- Clear Selection --
          </div>
          {#each group.items as item}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              class="pcr-atb-nsfw-item"
              class:selected={pendingSelections[group.name]?.tag === item.tag}
              onclick={() => selectItem(group.name, item)}
            >
              {item.display}
            </div>
          {/each}
        </div>
      {/each}
    </div>

    <div class="pcr-atb-nsfw-preview">
      <div class="pcr-atb-nsfw-preview-label">Selected:</div>
      <div class="pcr-atb-nsfw-preview-items">
        {#if selectedPills.length === 0}
          <span class="pcr-atb-nsfw-preview-none">None</span>
        {:else}
          {#each selectedPills as pill}
            <span class="pcr-atb-nsfw-preview-pill">{pill.display}</span>
          {/each}
        {/if}
      </div>
    </div>

    <div class="pcr-atb-customizer-footer">
      <button class="pcr-atb-customizer-btn pcr-atb-customizer-cancel" onclick={onCancel}>Cancel</button>
      <button class="pcr-atb-customizer-btn pcr-atb-customizer-ok" onclick={handleConfirm}>Done</button>
    </div>
  </div>
</div>
