<script>
  import { selection, prefs, viewMode, setViewMode, setSort, groupMode, setGroupMode, feedMode, favFilter, VIEW_MODES, SORT_FIELDS, GROUP_MODES } from "./stores.svelte.js";

  let {
    scope, scopes, searchQuery,
    onScopeChange, onSearchChange, onSortChange, onFeedToggle, onFavFilterToggle,
  } = $props();

  let openDd = $state(null); // "sort" | "view" | "group" | null
  let ddWrapEls = {};

  function toggleDd(name, e) {
    e.stopPropagation();
    openDd = openDd === name ? null : name;
  }

  function handleSortField(field) {
    const dir = field === prefs.sortField
      ? (prefs.sortDirection === "asc" ? "desc" : "asc")
      : (field === "modified" || field === "size" ? "desc" : "asc");
    setSort(field, dir);
    openDd = null;
    onSortChange?.();
  }

  function handleViewMode(mode) {
    setViewMode(mode);
    openDd = null;
  }

  function handleGroupMode(mode) {
    setGroupMode(mode);
    openDd = null;
    onSortChange?.();
  }

  let currentView = $derived(viewMode());
  let currentGroup = $derived(groupMode());
  let currentFeed = $derived(feedMode());
  let currentFav = $derived(favFilter());
</script>

<svelte:window onclick={(e) => {
  if (openDd) {
    const wrap = ddWrapEls[openDd];
    if (wrap && !wrap.contains(e.target)) openDd = null;
  }
}} />

<!-- scope tabs -->
<div class="pcr-tb-scopes">
  {#each scopes as s}
    <button
      class="pcr-tb-scope"
      class:active={scope === s.id}
      onclick={() => onScopeChange(s.id)}
    >{s.label}</button>
  {/each}
</div>

<!-- search + controls -->
<div class="pcr-tb-bar">
  <input
    class="pcr-tb-search"
    type="text" placeholder="Search..."
    value={searchQuery}
    oninput={(e) => onSearchChange(e.target.value)}
  />

  <!-- sort dropdown -->
  <div class="pcr-tb-dd-wrap" bind:this={ddWrapEls.sort}>
    <button
      class="pcr-tb-btn"
      disabled={currentFeed}
      title={currentFeed ? "Sorted by newest (feed)" : `Sort by ${prefs.sortField}`}
      onclick={(e) => toggleDd("sort", e)}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
        <line x1="4" y1="6" x2="16" y2="6"/><line x1="4" y1="12" x2="12" y2="12"/>
        <line x1="4" y1="18" x2="8" y2="18"/>
        <polyline points="15 15 18 18 21 15"/><line x1="18" y1="12" x2="18" y2="18"/>
      </svg>
    </button>
    {#if openDd === "sort"}
      <div class="pcr-tb-dd">
        {#each SORT_FIELDS as s}
          <button
            class="pcr-tb-dd-item"
            class:active={prefs.sortField === s.field}
            onclick={() => handleSortField(s.field)}
          >
            {s.label}
            {#if prefs.sortField === s.field}
              <span class="pcr-tb-dd-dir">{prefs.sortDirection === "asc" ? "\u2191" : "\u2193"}</span>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- view mode dropdown -->
  <div class="pcr-tb-dd-wrap" bind:this={ddWrapEls.view}>
    <button
      class="pcr-tb-btn"
      title="{currentView} view"
      onclick={(e) => toggleDd("view", e)}
    >
      {#if currentView === "grid"}
        <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14">
          <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
          <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
        </svg>
      {:else if currentView === "justified"}
        <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14">
          <rect x="2" y="3" width="9" height="6" rx="1"/><rect x="13" y="3" width="9" height="8" rx="1"/>
          <rect x="2" y="11" width="9" height="10" rx="1"/><rect x="13" y="13" width="9" height="8" rx="1"/>
        </svg>
      {:else}
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>
          <line x1="8" y1="18" x2="21" y2="18"/>
          <circle cx="4" cy="6" r="1" fill="currentColor"/>
          <circle cx="4" cy="12" r="1" fill="currentColor"/>
          <circle cx="4" cy="18" r="1" fill="currentColor"/>
        </svg>
      {/if}
    </button>
    {#if openDd === "view"}
      <div class="pcr-tb-dd">
        {#each VIEW_MODES as vm}
          <button
            class="pcr-tb-dd-item"
            class:active={currentView === vm.id}
            onclick={() => handleViewMode(vm.id)}
          >
            {vm.label}
            {#if currentView === vm.id}<span class="pcr-tb-dd-check">&#10003;</span>{/if}
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- group dropdown -->
  <div class="pcr-tb-dd-wrap" bind:this={ddWrapEls.group}>
    <button
      class="pcr-tb-btn"
      class:active={currentGroup !== "none"}
      title="Group by {currentGroup}"
      onclick={(e) => toggleDd("group", e)}
    >
      <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14">
        <path d="M5 3a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2H5Zm0 12a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2H5Zm12 0a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2h-2Zm0-12a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-2Z"/>
        <path fill-rule="evenodd" d="M10 6.5a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1ZM10 18a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1Zm-4-4a1 1 0 0 1-1-1v-2a1 1 0 1 1 2 0v2a1 1 0 0 1-1 1Zm12 0a1 1 0 0 1-1-1v-2a1 1 0 1 1 2 0v2a1 1 0 0 1-1 1Z" clip-rule="evenodd"/>
      </svg>
    </button>
    {#if openDd === "group"}
      <div class="pcr-tb-dd">
        {#each GROUP_MODES as g}
          <button
            class="pcr-tb-dd-item"
            class:active={currentGroup === g.id}
            onclick={() => handleGroupMode(g.id)}
          >
            {g.label}
            {#if currentGroup === g.id}<span class="pcr-tb-dd-check">&#10003;</span>{/if}
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <!-- starred-only filter -->
  <button
    class="pcr-tb-btn"
    class:active={currentFav}
    title={currentFav ? "Showing starred only" : "Show starred only"}
    onclick={() => onFavFilterToggle?.()}
  >
    <svg viewBox="0 0 24 24" fill={currentFav ? "currentColor" : "none"} stroke="currentColor" stroke-width="2" stroke-linejoin="round" width="14" height="14">
      <polygon points="12 2.6 15 8.8 21.8 9.7 16.9 14.4 18.1 21.2 12 18 5.9 21.2 7.1 14.4 2.2 9.7 9 8.8"/>
    </svg>
  </button>

  <!-- recent-feed toggle (folder browsing vs flat newest-first subtree) -->
  <button
    class="pcr-tb-btn"
    class:active={currentFeed}
    title="Recent feed — all subfolders, newest first"
    onclick={() => onFeedToggle?.()}
  >
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
      <circle cx="12" cy="12" r="9"/>
      <polyline points="12 7 12 12 15.5 14"/>
    </svg>
  </button>
</div>

<style>
  .pcr-tb-scopes {
    display: flex;
    border-bottom: 1px solid var(--border-color, #333);
    flex-shrink: 0;
  }
  .pcr-tb-scope {
    flex: 1; padding: 6px 4px; border: none; background: transparent;
    color: #ffffffbf; cursor: pointer; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .pcr-tb-scope:hover { background: transparent; color: #ff8a25; }
  .pcr-tb-scope.active {
    color: #ff8a25;
    border-bottom: 2px solid #f36b00;
  }

  .pcr-tb-bar {
    display: flex; gap: 3px; padding: 8px 8px; align-items: center;
    border-bottom: 1px solid var(--border-color, #333); flex-shrink: 0;
  }
  .pcr-tb-search {
    flex: 1; min-width: 0; padding: 4px 6px;
    border: 1px solid var(--border-color, #333); border-radius: 3px;
    background: rgba(0, 0, 0, 0.2); color: var(--input-text, #ccc);
    font-size: 12px; outline: none;
  }
  .pcr-tb-search:focus {
    border-color: var(--p-button-text-primary-color, #4fc3f7);
  }
  .pcr-tb-search::placeholder { color: var(--input-text, #555); }

  .pcr-tb-btn {
    display: flex; align-items: center; justify-content: center;
    width: 24px; height: 24px; padding: 0;
    border: none; border-radius: 3px; background: transparent;
    color: var(--input-text, #888); cursor: pointer;
  }
  .pcr-tb-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    color: var(--input-text, #fff);
  }
  .pcr-tb-btn.active {
    color: #dd7634;
  }
  .pcr-tb-btn:disabled {
    opacity: 0.35; cursor: default;
  }
  .pcr-tb-btn:disabled:hover {
    background: transparent;
    color: var(--input-text, #888);
  }

  .pcr-tb-dd-wrap { position: relative; }
  .pcr-tb-dd {
    position: absolute; top: 100%; right: 0; z-index: 100;
    min-width: 100px; margin-top: 2px;
    background: var(--comfy-menu-bg, #2a2a2a);
    border: 1px solid var(--border-color, #444);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  }
  .pcr-tb-dd-item {
    display: flex; justify-content: space-between; width: 100%;
    padding: 6px 10px; border: none; background: transparent;
    color: var(--input-text, #ccc); font-size: 11px;
    text-align: left; cursor: pointer;
  }
  .pcr-tb-dd-item:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-tb-dd-item.active {
    color: var(--p-button-text-primary-color, #4fc3f7);
  }
  .pcr-tb-dd-dir { font-size: 10px; }
  .pcr-tb-dd-check { font-size: 10px; }
</style>
