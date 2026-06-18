<script>
  // CharacterModal -- modal for selecting character options (base appearance, outfit, pose).

  let {
    characterTag,
    characterDisplay,
    existing = null,
    cache = {},
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let loading = $state(true);
  let char = $state(null);
  let includeBase = $state(existing ? !!existing.base : true);
  let outfitIndex = $state(existing && existing.outfitIndex != null ? existing.outfitIndex : "");
  let poseIndex = $state(existing && existing.poseIndex != null ? existing.poseIndex : "");

  $effect(() => {
    loadCharacter(characterTag);
  });

  async function loadCharacter(tag) {
    const cacheKey = `char:${tag}`;
    if (cache[cacheKey]) {
      char = cache[cacheKey];
      loading = false;
      return;
    }

    try {
      const response = await fetch(`/promptchain/tag-builder/characters/${encodeURIComponent(tag)}`);
      if (!response.ok) throw new Error("Failed to fetch");
      const data = await response.json();
      cache[cacheKey] = data;
      char = data;
    } catch (e) {
      console.error("[TagBuilder] Failed to load character:", e);
      onCancel();
      return;
    }
    loading = false;
  }

  function handleConfirm() {
    if (!char) return;

    const outIdx = outfitIndex !== "" ? parseInt(outfitIndex) : null;
    const posIdx = poseIndex !== "" ? parseInt(poseIndex) : null;

    const outfit = outIdx !== null && char.outfits?.[outIdx]
      ? {
          tags: char.outfits[outIdx].outfit_tags || "",
          natlang: char.outfits[outIdx].outfit_natlang || "",
          display: char.outfits[outIdx].outfit_name + (char.outfits[outIdx].is_default ? " (default)" : ""),
          overridesTags: char.outfits[outIdx].overrides_tags || "",
          overridesNatlang: char.outfits[outIdx].overrides_natlang || "",
          slots: char.outfits[outIdx].slots || [],
        }
      : null;

    const pose = posIdx !== null && char.poses?.[posIdx]
      ? {
          tags: char.poses[posIdx].pose_tags || "",
          natlang: char.poses[posIdx].pose_natlang || "",
          display: char.poses[posIdx].pose_name + (char.poses[posIdx].is_signature ? " (signature)" : ""),
        }
      : null;

    onConfirm({
      tag: char.tag,
      display: char.display || char.tag,
      base: includeBase,
      baseTags: char.base_tags || "",
      baseNatlang: char.base_natlang || "",
      outfitIndex: outIdx,
      outfit,
      poseIndex: posIdx,
      pose,
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

  // Autofocus the confirm button when the modal renders — Enter then naturally
  // activates it without relying on the overlay div holding focus.
  function focusOnMount(node) { node.focus(); }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="pcr-atb-customizer-overlay" onclick={handleOverlayClick} onkeydown={handleKeydown}>
  {#if loading}
    <div class="pcr-atb-customizer-modal">
      <div class="pcr-atb-loading">Loading character...</div>
    </div>
  {:else if char}
    <div class="pcr-atb-customizer-modal pcr-atb-char-modal">
      <div class="pcr-atb-customizer-header">
        <span class="pcr-atb-customizer-title">{char.display || char.tag}</span>
        {#if char.series}
          <span class="pcr-atb-char-modal-series">{char.series}</span>
        {/if}
        <button class="pcr-atb-customizer-close" onclick={onCancel}>&times;</button>
      </div>

      <div class="pcr-atb-customizer-body">
        <div class="pcr-atb-customizer-row">
          <label class="pcr-atb-checkbox">
            <input type="checkbox" bind:checked={includeBase}>
            <span>Base appearance</span>
          </label>
        </div>

        {#if char.outfits?.length}
          <div class="pcr-atb-customizer-row">
            <label>Outfit:</label>
            <select class="pcr-atb-customizer-select" bind:value={outfitIndex}>
              <option value="">-- None --</option>
              {#each char.outfits as outfit, i}
                <option value={i}>{outfit.outfit_name}{outfit.is_default ? " (default)" : ""}</option>
              {/each}
            </select>
          </div>
        {/if}

        {#if char.poses?.length}
          <div class="pcr-atb-customizer-row">
            <label>Pose:</label>
            <select class="pcr-atb-customizer-select" bind:value={poseIndex}>
              <option value="">-- None --</option>
              {#each char.poses as pose, i}
                <option value={i}>{pose.pose_name}{pose.is_signature ? " (signature)" : ""}</option>
              {/each}
            </select>
          </div>
        {/if}
      </div>

      <div class="pcr-atb-customizer-footer">
        <button class="pcr-atb-customizer-btn pcr-atb-customizer-cancel" onclick={onCancel}>Cancel</button>
        <button class="pcr-atb-customizer-btn pcr-atb-customizer-ok" onclick={handleConfirm} use:focusOnMount>{existing ? "Update Character" : "Add Character"}</button>
      </div>
    </div>
  {/if}
</div>
