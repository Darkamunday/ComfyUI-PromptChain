<script>
  // ClothingPanel -- pill-filtered sectioned layout. Default state shows every
  // group as its own section with header + items grid. Clicking a pill toggles
  // that group into a filter set; only filtered groups render. Empty filter
  // set means "show everything." No virtual "All" pill — clicking active pills
  // off naturally returns to the all-sections-visible state.

  import { onMount } from "svelte";
  import { CUSTOMIZABLE_CLOTHING_GROUPS } from "../../lib/tag-builder-constants.js";

  let {
    groups = [],
    selections = {},
    isNaturalMode = false,
    searchQuery = "",
    onSelect = () => {},
    onOpenClothingCustomizer = null,
  } = $props();

  let activeFilters = $state(new Set());
  let availableThumbs = $state(new Set());

  onMount(async () => {
    try {
      const res = await fetch("/promptchain/tag-builder/thumbs/manifest?bucket=clothing");
      if (!res.ok) return;
      const data = await res.json();
      availableThumbs = new Set(data.thumbs || []);
    } catch {
      // No manifest -- everything stays in pill mode.
    }
  });

  function togglePill(groupName) {
    const next = new Set(activeFilters);
    if (next.has(groupName)) next.delete(groupName);
    else next.add(groupName);
    activeFilters = next;
  }

  let visibleGroups = $derived.by(() =>
    activeFilters.size === 0 ? groups : groups.filter(g => activeFilters.has(g.name))
  );

  function isCustomizable(groupName) {
    return CUSTOMIZABLE_CLOTHING_GROUPS.includes((groupName || "").toLowerCase());
  }

  function getSelectedTags(groupName) {
    const sel = selections[groupName];
    if (!sel) return new Set();
    const items = Array.isArray(sel) ? sel : [sel];
    return new Set(items.map(s => s.tag));
  }

  function getGroupSelectionCount(groupName) {
    const sel = selections[groupName];
    if (!sel) return 0;
    return Array.isArray(sel) ? sel.length : 1;
  }

  function thumbUrl(itemTag) {
    return `/promptchain/tag-builder/thumb/clothing/${encodeURIComponent(itemTag)}`;
  }

  function categoryThumbUrl(groupName) {
    return `/promptchain/tag-builder/thumb/clothing/${encodeURIComponent("_group_" + groupName)}`;
  }

  function hasItemThumb(itemTag) {
    return availableThumbs.has(itemTag);
  }

  function hasGroupThumb(groupName) {
    return availableThumbs.has("_group_" + groupName);
  }

  function handleAddItem(item, groupName) {
    if (isCustomizable(groupName) && onOpenClothingCustomizer) {
      onOpenClothingCustomizer({
        tag: item.tag,
        display: item.display || item.tag,
        tags: item.tags,
        natlang: item.natlang,
        group: groupName.toLowerCase(),
      });
    } else {
      onSelect("clothing", groupName, item, "single");
    }
  }

  function handleClearGroup(groupName) {
    onSelect("clothing", groupName, null, "clear");
  }

  function filterItems(items) {
    const q = (searchQuery || "").toLowerCase().replace(/[_\s]+/g, " ").trim();
    if (!q) return items;
    return items.filter(item => {
      const d = (item.display || item.tag || "").toLowerCase().replace(/[_\s]+/g, " ");
      const t = (item.tags || item.tag || "").toLowerCase().replace(/[_\s]+/g, " ");
      return d.includes(q) || t.includes(q);
    });
  }

  let visibleGroupsWithItems = $derived.by(() =>
    visibleGroups.map(g => ({ group: g, items: filterItems(g.items) })).filter(x => x.items.length > 0)
  );
</script>

<div class="pcr-tb-clothing">
  <div class="pcr-tb-clothing-categories">
    {#each groups as group}
      {@const isActive = activeFilters.has(group.name)}
      {@const selectedCount = getGroupSelectionCount(group.name)}
      {@const showThumb = hasGroupThumb(group.name)}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-tb-clothing-cat"
        class:active={isActive}
        class:has-selection={selectedCount > 0}
        class:has-thumb={showThumb}
        onclick={() => togglePill(group.name)}
      >
        {#if showThumb}
          <div class="pcr-tb-clothing-cat-thumb">
            <img src={categoryThumbUrl(group.name)} alt="" loading="lazy" />
          </div>
        {/if}
        <div class="pcr-tb-clothing-cat-meta">
          <div class="pcr-tb-clothing-cat-name">{group.display || group.name}</div>
          <div class="pcr-tb-clothing-cat-count">
            {group.items.length}
            {#if selectedCount > 0}
              <span class="dot">·</span><span class="sel">{selectedCount} selected</span>
            {/if}
          </div>
        </div>
      </div>
    {/each}
  </div>

  {#each visibleGroupsWithItems as { group, items }}
    <div class="pcr-tb-clothing-section">
      <div class="pcr-tb-clothing-section-header">
        <span class="title">{group.display || group.name}</span>
        <span class="count">{items.length}</span>
        {#if getGroupSelectionCount(group.name) > 0}
          <button class="pcr-tb-clothing-clear" onclick={() => handleClearGroup(group.name)}>Clear</button>
        {/if}
      </div>
      <div class="pcr-tb-clothing-items">
        {#each items as item}
          {@const showThumb = hasItemThumb(item.tag)}
          {@const isSelected = getSelectedTags(group.name).has(item.tag)}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="pcr-tb-clothing-item"
            class:selected={isSelected}
            class:has-thumb={showThumb}
            onclick={() => handleAddItem(item, group.name)}
            title={item.display || item.tag}
          >
            {#if showThumb}
              <div class="pcr-tb-clothing-item-thumb">
                <img src={thumbUrl(item.tag)} alt={item.display || item.tag} loading="lazy" />
              </div>
            {/if}
            <div class="pcr-tb-clothing-item-name">{item.display || item.tag}</div>
            <button
              type="button"
              class="pcr-tb-clothing-item-add"
              aria-label="Add"
              onclick={(e) => { e.stopPropagation(); handleAddItem(item, group.name); }}
            >+</button>
          </div>
        {/each}
      </div>
    </div>
  {/each}

  {#if visibleGroupsWithItems.length === 0}
    <div class="pcr-tb-clothing-empty">
      {searchQuery ? `No matches for "${searchQuery}"` : "No items"}
    </div>
  {/if}
</div>
