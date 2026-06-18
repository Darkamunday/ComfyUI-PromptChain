<script>
  // SearchableList — keyboard-navigable filtered option list.
  // Port of createSearchableList() from popup-menu.js.

  let {
    options = [],
    onSelect = () => {},
    currentMode = "switch",
    currentSwitchIndex = 1,
    itemPrefix = "",
  } = $props();

  let filter = $state("");
  let selectedIndex = $state(-1);
  let searchInput = $state(null);

  let filtered = $derived.by(() => {
    const terms = filter.toLowerCase().split(/\s+/).filter(t => t.length > 0);
    if (!terms.length) return options;
    return options.filter(opt =>
      terms.every(t => opt.label.toLowerCase().includes(t))
    );
  });

  // reset selection when filter changes
  $effect(() => {
    filter;
    selectedIndex = -1;
  });

  function highlightText(text, terms) {
    if (!terms.length) return text;
    let result = "";
    let remaining = text;
    let lower = remaining.toLowerCase();
    while (remaining.length > 0) {
      let earliest = -1;
      let matched = "";
      for (const term of terms) {
        const idx = lower.indexOf(term);
        if (idx !== -1 && (earliest === -1 || idx < earliest)) {
          earliest = idx;
          matched = term;
        }
      }
      if (earliest === -1) {
        result += escapeHtml(remaining);
        break;
      }
      if (earliest > 0) result += escapeHtml(remaining.slice(0, earliest));
      result += `<span class="pcr-mode-menu-highlight">${escapeHtml(remaining.slice(earliest, earliest + matched.length))}</span>`;
      remaining = remaining.slice(earliest + matched.length);
      lower = remaining.toLowerCase();
    }
    return result;
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  let searchTerms = $derived(filter.toLowerCase().split(/\s+/).filter(t => t.length > 0));

  function handleKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (filtered.length > 0) selectedIndex = (selectedIndex + 1) % filtered.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (filtered.length > 0) selectedIndex = selectedIndex > 0 ? selectedIndex - 1 : filtered.length - 1;
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (selectedIndex >= 0 && selectedIndex < filtered.length) {
        onSelect(filtered[selectedIndex]);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      // close handled by PopupAnchor
    }
    e.stopPropagation();
  }

  export function focusSearch() {
    requestAnimationFrame(() => searchInput?.focus());
  }
</script>

{#if options.length > 0}
  <div class="pcr-mode-menu-search-container">
    <input
      bind:this={searchInput}
      bind:value={filter}
      type="text"
      class="pcr-mode-menu-search"
      placeholder="Search options..."
      onkeydown={handleKeydown}
    />
  </div>
  <div class="pcr-mode-menu-separator"></div>
{/if}

<div class="pcr-mode-menu-list">
  {#if filtered.length === 0 && options.length > 0}
    <div class="pcr-mode-menu-empty">No matching options</div>
  {:else}
    {#each filtered as opt, i}
      {@const isSelected = currentMode === "switch" && opt.index === currentSwitchIndex}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-mode-menu-item"
        class:pcr-mode-menu-selected={isSelected}
        class:pcr-mode-menu-keyboard-selected={i === selectedIndex}
        onclick={(e) => { e.stopPropagation(); onSelect(opt); }}
        onmouseenter={() => { selectedIndex = i; }}
      >
        <span class="pcr-mode-menu-label">
          {@html highlightText(`${itemPrefix}${opt.label}`, searchTerms)}
        </span>
        {#if isSelected}
          <span class="pcr-mode-menu-check">{"\u2713"}</span>
        {/if}
      </div>
    {/each}
  {/if}
</div>
