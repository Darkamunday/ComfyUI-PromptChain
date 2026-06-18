<script>
  // MixerGrid -- reusable dropdown grid for mixer buckets (appearance, clothing, pose, etc.).
  // Each group renders as a collapsible header with a dropdown of items.

  import { MULTI_SELECT_GROUPS, CUSTOMIZABLE_CLOTHING_GROUPS } from "../../lib/tag-builder-constants.js";

  let {
    groups = [],
    bucket = "",
    selections = {},
    isNaturalMode = false,
    searchQuery = "",
    openGroup = null,
    onSetOpenGroup = () => {},
    dropdownSearchQuery = "",
    onSetDropdownSearchQuery = () => {},
    highlightedIndex = 0,
    onSetHighlightedIndex = () => {},
    onSelect = () => {},
    onOpenNsfwModal = null,
    onOpenClothingCustomizer = null,
    onOpenFantasyCustomizer = null,
  } = $props();

  // Focus action: auto-focus the search input the moment the dropdown opens.
  function focusOnMount(node) { node.focus(); }

  function highlightScroll(node, highlighted) {
    function check(h) { if (h) node.scrollIntoView({ block: "nearest" }); }
    check(highlighted);
    return { update: check };
  }

  function filterOpenItems(items) {
    const q = (dropdownSearchQuery || "").toLowerCase().replace(/[_\s]+/g, " ").trim();
    if (!q) return items;
    return items.filter(item => {
      const d = (item.display || item.tag || "").toLowerCase().replace(/[_\s]+/g, " ");
      const t = (item.tags || item.tag || "").toLowerCase().replace(/[_\s]+/g, " ");
      return d.includes(q) || t.includes(q);
    });
  }

  function handleSearchKey(e, flatList, group) {
    // Escape is owned by TagBuilder's document-level handler.
    if (e.key === "ArrowDown") { e.preventDefault(); onSetHighlightedIndex(Math.min(Math.max(flatList.length - 1, 0), highlightedIndex + 1)); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); onSetHighlightedIndex(Math.max(0, highlightedIndex - 1)); return; }
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); const item = flatList[highlightedIndex]; if (item) handleOptionClick(e, group, item); return; }
  }

  let filteredGroups = $derived.by(() => {
    const lowerQuery = (searchQuery || "").toLowerCase();
    if (!lowerQuery) return groups;

    return groups
      .map(group => {
        const filtered = group.items.filter(item => {
          const normalizedDisplay = (item.display || item.tag).toLowerCase().replace(/[_\s]+/g, " ");
          const normalizedTag = (item.tags || "").toLowerCase().replace(/[_\s]+/g, " ");
          const normalizedQuery = lowerQuery.replace(/[_\s]+/g, " ");
          return normalizedDisplay.includes(normalizedQuery) || normalizedTag.includes(normalizedQuery);
        });
        return { ...group, items: filtered };
      })
      .filter(group => group.items.length > 0);
  });

  // Svelte action for positioning dropdown below or above the header
  function positionDropdown(dropdownNode) {
    function reposition() {
      if (!dropdownNode || !dropdownNode.isConnected) return;
      const groupEl = dropdownNode.closest(".pcr-atb-mixer-group");
      if (!groupEl) return;
      const header = groupEl.querySelector(".pcr-atb-mixer-header");
      if (!header) return;

      // Fixed positioning only applies inside the All tab wrapper
      const isFixed = !!groupEl.closest(".pcr-atb-all-mixer-wrapper");
      if (!isFixed) {
        // Absolute positioning — CSS handles it via top:100%, just manage flip
        const headerRect = header.getBoundingClientRect();
        const isCharCategory = groupEl.classList.contains("pcr-atb-char-category");
        const maxDropdownHeight = isCharCategory ? 350 : 200;
        const dropdownHeight = Math.min(dropdownNode.scrollHeight || maxDropdownHeight, maxDropdownHeight);
        const viewportHeight = window.innerHeight;
        const spaceBelow = viewportHeight - headerRect.bottom;
        const spaceAbove = headerRect.top;

        if (spaceBelow < dropdownHeight && spaceAbove > spaceBelow) {
          groupEl.classList.add("flip-up");
          dropdownNode.style.maxHeight = `${Math.min(spaceAbove - 10, maxDropdownHeight)}px`;
        } else {
          groupEl.classList.remove("flip-up");
          dropdownNode.style.maxHeight = `${Math.min(spaceBelow - 10, maxDropdownHeight)}px`;
        }
        return;
      }

      // Fixed positioning for All tab
      const headerRect = header.getBoundingClientRect();
      const isCharCategory = groupEl.classList.contains("pcr-atb-char-category");
      const maxDropdownHeight = isCharCategory ? 350 : 200;
      const dropdownHeight = Math.min(dropdownNode.scrollHeight || maxDropdownHeight, maxDropdownHeight);
      const viewportHeight = window.innerHeight;
      const spaceBelow = viewportHeight - headerRect.bottom;
      const spaceAbove = headerRect.top;

      if (spaceBelow < dropdownHeight && spaceAbove > spaceBelow) {
        groupEl.classList.add("flip-up");
        dropdownNode.style.left = `${headerRect.left}px`;
        dropdownNode.style.width = `${headerRect.width}px`;
        dropdownNode.style.bottom = `${viewportHeight - headerRect.top}px`;
        dropdownNode.style.top = "auto";
        dropdownNode.style.maxHeight = `${Math.min(spaceAbove - 10, maxDropdownHeight)}px`;
      } else {
        groupEl.classList.remove("flip-up");
        dropdownNode.style.left = `${headerRect.left}px`;
        dropdownNode.style.width = `${headerRect.width}px`;
        dropdownNode.style.top = `${headerRect.bottom}px`;
        dropdownNode.style.bottom = "auto";
        dropdownNode.style.maxHeight = `${Math.min(spaceBelow - 10, maxDropdownHeight)}px`;
      }
    }

    reposition();
    // Fixed-position dropdowns (All tab) need re-anchoring when the content
    // area scrolls or the window resizes; absolute-position ones don't but
    // the listener is harmless.
    const scrollContainer = dropdownNode.closest(".pcr-atb-content");
    if (scrollContainer) scrollContainer.addEventListener("scroll", reposition, { passive: true });
    window.addEventListener("resize", reposition);
    return {
      update: reposition,
      destroy() {
        if (scrollContainer) scrollContainer.removeEventListener("scroll", reposition);
        window.removeEventListener("resize", reposition);
      },
    };
  }

  function isMultiSelect(groupName) {
    return MULTI_SELECT_GROUPS.includes(groupName.toLowerCase());
  }

  function getSelectedItems(groupName) {
    const sel = selections[groupName];
    if (!sel) return [];
    return Array.isArray(sel) ? sel : [sel];
  }

  function getSelectedTags(groupName) {
    return new Set(getSelectedItems(groupName).map(s => s.tag));
  }

  function getDisplayValue(groupName) {
    const items = getSelectedItems(groupName);
    if (items.length === 0) return "Select";
    if (items.length === 1) return items[0].display;
    return `${items.length} selected`;
  }

  function hasValue(groupName) {
    return getSelectedItems(groupName).length > 0;
  }

  function toggleGroup(groupName) {
    onSetOpenGroup(openGroup === groupName ? null : groupName);
  }

  function handleOptionClick(e, group, item) {
    e.stopPropagation();
    const groupName = group.name;

    // Customizable clothing -- delegate to modal
    if (bucket === "clothing" && CUSTOMIZABLE_CLOTHING_GROUPS.includes(groupName.toLowerCase()) && item) {
      onSetOpenGroup(null);
      if (onOpenClothingCustomizer) {
        onOpenClothingCustomizer({
          tag: item.tag,
          display: item.display || item.tag,
          tags: item.tags,
          natlang: item.natlang,
          group: groupName.toLowerCase(),
        });
      }
      return;
    }

    // Fantasy features -- delegate to modal
    if (bucket === "appearance" && groupName.toLowerCase() === "fantasy" && item) {
      onSetOpenGroup(null);
      if (onOpenFantasyCustomizer) {
        onOpenFantasyCustomizer({
          tag: item.tag,
          display: item.display || item.tag,
          tags: item.tags,
          natlang: item.natlang,
        });
      }
      return;
    }

    // Standard select behavior
    if (isMultiSelect(groupName)) {
      // Toggle item in multi-select mode
      onSelect(bucket, groupName, item, "toggle");
    } else {
      // Single select
      onSelect(bucket, groupName, item, "single");
      if (item) onSetOpenGroup(null);
    }
  }

  function handleClearGroup(e, group) {
    e.stopPropagation();
    onSelect(bucket, group.name, null, "clear");
    if (!isMultiSelect(group.name)) {
      onSetOpenGroup(null);
    }
  }

  function handleAdultClick(e) {
    e.stopPropagation();
    onSetOpenGroup(null);
    if (onOpenNsfwModal) onOpenNsfwModal();
  }

</script>

<div class="pcr-atb-mixer-grid">
  {#each filteredGroups as group}
    {@const multi = isMultiSelect(group.name)}
    {@const selectedTags = getSelectedTags(group.name)}
    {@const isOpen = openGroup === group.name}
    {@const visibleItems = isOpen ? filterOpenItems(group.items) : []}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="pcr-atb-mixer-group"
      class:open={isOpen}
      class:multi-select={multi}
      data-group={group.name}
    >
      <div class="pcr-atb-mixer-header" onclick={isOpen ? null : (e) => { e.stopPropagation(); toggleGroup(group.name); }}>
        {#if isOpen}
          <div class="pcr-atb-mixer-search-wrap">
            <input
              class="pcr-atb-mixer-search"
              type="text"
              placeholder="Search {group.display || group.name}…"
              value={dropdownSearchQuery}
              oninput={(e) => onSetDropdownSearchQuery(e.target.value)}
              onclick={(e) => e.stopPropagation()}
              onkeydown={(e) => handleSearchKey(e, visibleItems, group)}
              use:focusOnMount
            />
            <button
              type="button"
              class="pcr-atb-mixer-close"
              aria-label="Close"
              onclick={(e) => { e.stopPropagation(); onSetOpenGroup(null); }}
            >&times;</button>
          </div>
        {:else}
          <span class="pcr-atb-mixer-label">{group.display || group.name}</span>
          <span class="pcr-atb-mixer-value" class:has-value={hasValue(group.name)}>
            {getDisplayValue(group.name)}
          </span>
        {/if}
      </div>
      {#if isOpen}
        <div class="pcr-atb-mixer-dropdown" use:positionDropdown>
          <div
            class="pcr-atb-mixer-option pcr-atb-mixer-none"
            onclick={(e) => handleClearGroup(e, group)}
          >
            -- {multi ? "Clear All" : "None"} --
          </div>
          {#each visibleItems as item, idx}
            {@const isHighlighted = idx === highlightedIndex}
            <div
              class="pcr-atb-mixer-option"
              class:selected={selectedTags.has(item.tag)}
              class:highlighted={isHighlighted}
              onclick={(e) => handleOptionClick(e, group, item)}
              use:highlightScroll={isHighlighted}
            >
              {#if multi}
                <span class="pcr-atb-checkbox-icon">{selectedTags.has(item.tag) ? "\u2611" : "\u2610"}</span>
              {/if}
              {item.display || item.tag}
            </div>
          {/each}
          {#if visibleItems.length === 0}
            <div class="pcr-atb-mixer-empty">No matches</div>
          {/if}
        </div>
      {/if}
    </div>
  {/each}

  {#if bucket === "action" && onOpenNsfwModal}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="pcr-atb-mixer-group pcr-atb-adult-trigger">
      <div class="pcr-atb-mixer-header pcr-atb-adult-header" onclick={handleAdultClick}>
        <span class="pcr-atb-mixer-label">Adult Actions</span>
        <span class="pcr-atb-mixer-value" class:has-value={false}>Browse</span>
      </div>
    </div>
  {/if}
</div>
