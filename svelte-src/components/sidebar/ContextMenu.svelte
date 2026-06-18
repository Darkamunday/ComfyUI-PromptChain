<script>
  import { selection, clipboard, viewMode, setViewMode, prefs, setSort, groupMode, setGroupMode, VIEW_MODES, SORT_FIELDS, GROUP_MODES } from "./stores.svelte.js";

  let {
    x, y, scope, targetItem, feed = false,
    onAction, onClose,
  } = $props();

  let menuEl;
  let openSub = $state(null);

  let selCount = $derived(selection.items.size);
  let isItemMenu = $derived(!!targetItem);
  let currentView = $derived(viewMode());
  let currentGroup = $derived(groupMode());

  function act(action) {
    onAction?.(action);
    onClose?.();
  }

  function handleSort(field) {
    const dir = field === prefs.sortField
      ? (prefs.sortDirection === "asc" ? "desc" : "asc")
      : (field === "modified" || field === "size" ? "desc" : "asc");
    setSort(field, dir);
    onAction?.("refresh");
    onClose?.();
  }

  let pos = $derived((() => {
    const mw = 180, mh = 400;
    const vw = window.innerWidth, vh = window.innerHeight;
    return {
      left: Math.min(x, vw - mw - 8),
      top: Math.min(y, vh - mh - 8),
    };
  })());

  $effect(() => {
    function onClick(e) {
      if (menuEl && !menuEl.contains(e.target)) onClose?.();
    }
    function onKey(e) {
      if (e.key === "Escape") onClose?.();
    }
    window.addEventListener("click", onClick, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", onClick, true);
      window.removeEventListener("keydown", onKey);
    };
  });
</script>

<div class="pcr-ctx" bind:this={menuEl} style="left:{pos.left}px;top:{pos.top}px;">
  {#if isItemMenu}
    {#if targetItem.type === "image"}
      <button class="pcr-ctx-item" onclick={() => act("edit")}>Edit</button>
      <div class="pcr-ctx-sep"></div>
    {/if}
    {#if feed}
      <button class="pcr-ctx-item" onclick={() => act("locate")}>Open file location</button>
      <div class="pcr-ctx-sep"></div>
    {/if}
    <button class="pcr-ctx-item" onclick={() => act("favorite")}>
      {targetItem.favorite ? "Remove from favorites" : "Add to favorites"}
    </button>
    <button class="pcr-ctx-item" onclick={() => act("cut")}>Cut</button>
    <button class="pcr-ctx-item" onclick={() => act("copy")}>Copy</button>
    <button class="pcr-ctx-item" onclick={() => act("copy-path")}>Copy as path</button>
    {#if clipboard.items.length > 0}
      <button class="pcr-ctx-item" onclick={() => act("paste")}>Paste</button>
    {/if}
    <div class="pcr-ctx-sep"></div>
    {#if selCount > 1}
      <button class="pcr-ctx-item" onclick={() => act("delete")}>Delete {selCount} items</button>
    {:else}
      <button class="pcr-ctx-item" onclick={() => act("rename")}>Rename</button>
      <button class="pcr-ctx-item" onclick={() => act("delete")}>Delete</button>
    {/if}
    <div class="pcr-ctx-sep"></div>
    <button class="pcr-ctx-item" onclick={() => act("refresh")}>Refresh</button>
    {#if selCount <= 1}
      <button class="pcr-ctx-item" onclick={() => act("properties")}>Properties</button>
    {/if}
  {:else}
    {#if clipboard.items.length > 0}
      <button class="pcr-ctx-item" onmouseenter={() => openSub = null} onclick={() => act("paste")}>
        Paste {clipboard.items.length} item{clipboard.items.length > 1 ? "s" : ""}
      </button>
      <div class="pcr-ctx-sep"></div>
    {/if}

    <!-- view submenu -->
    <div class="pcr-ctx-sub-wrap"
      onmouseenter={() => openSub = "view"}
    >
      <button class="pcr-ctx-item pcr-ctx-has-sub">
        <svg class="pcr-ctx-icon" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        View
        <span class="pcr-ctx-chevron">&#9656;</span>
      </button>
      {#if openSub === "view"}
        <div class="pcr-ctx-sub">
          {#each VIEW_MODES as vm}
            <button class="pcr-ctx-item" class:active={currentView === vm.id} onclick={() => { setViewMode(vm.id); onClose?.(); }}>
              {vm.label}
              {#if currentView === vm.id}<span class="pcr-ctx-check">&#10003;</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>

    <!-- sort submenu (feed order is fixed newest-first) -->
    {#if !feed}
    <div class="pcr-ctx-sub-wrap"
      onmouseenter={() => openSub = "sort"}
    >
      <button class="pcr-ctx-item pcr-ctx-has-sub">
        <svg class="pcr-ctx-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="6" x2="16" y2="6"/><line x1="4" y1="12" x2="12" y2="12"/><line x1="4" y1="18" x2="8" y2="18"/><polyline points="15 15 18 18 21 15"/><line x1="18" y1="12" x2="18" y2="18"/></svg>
        Sort by
        <span class="pcr-ctx-chevron">&#9656;</span>
      </button>
      {#if openSub === "sort"}
        <div class="pcr-ctx-sub">
          {#each SORT_FIELDS as s}
            <button class="pcr-ctx-item" class:active={prefs.sortField === s.field} onclick={() => handleSort(s.field)}>
              {s.label}
              {#if prefs.sortField === s.field}<span class="pcr-ctx-dir">{prefs.sortDirection === "asc" ? "\u2191" : "\u2193"}</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>
    {/if}

    <!-- group submenu -->
    <div class="pcr-ctx-sub-wrap"
      onmouseenter={() => openSub = "group"}
    >
      <button class="pcr-ctx-item pcr-ctx-has-sub">
        <svg class="pcr-ctx-icon" viewBox="0 0 24 24" fill="currentColor"><path d="M5 3a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2H5Zm0 12a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2H5Zm12 0a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2h-2Zm0-12a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-2Z"/><path fill-rule="evenodd" d="M10 6.5a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1ZM10 18a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1Zm-4-4a1 1 0 0 1-1-1v-2a1 1 0 1 1 2 0v2a1 1 0 0 1-1 1Zm12 0a1 1 0 0 1-1-1v-2a1 1 0 1 1 2 0v2a1 1 0 0 1-1 1Z" clip-rule="evenodd"/></svg>
        Group by
        <span class="pcr-ctx-chevron">&#9656;</span>
      </button>
      {#if openSub === "group"}
        <div class="pcr-ctx-sub">
          {#each GROUP_MODES as g}
            <button class="pcr-ctx-item" class:active={currentGroup === g.id} onclick={() => { setGroupMode(g.id); onAction?.("refresh"); onClose?.(); }}>
              {g.label}
              {#if currentGroup === g.id}<span class="pcr-ctx-check">&#10003;</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>

    <div class="pcr-ctx-sep"></div>

    <!-- new submenu -->
    <div class="pcr-ctx-sub-wrap"
      onmouseenter={() => openSub = "new"}
    >
      <button class="pcr-ctx-item pcr-ctx-has-sub">
        <svg class="pcr-ctx-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        New
        <span class="pcr-ctx-chevron">&#9656;</span>
      </button>
      {#if openSub === "new"}
        <div class="pcr-ctx-sub">
          <!-- a folder created in feed mode would be invisible (feed lists files only) -->
          {#if !feed}
            <button class="pcr-ctx-item" onclick={() => act("mkdir")}>Folder</button>
          {/if}
          <button class="pcr-ctx-item" onclick={() => act("new-workflow")}>Workflow</button>
        </div>
      {/if}
    </div>

    <div class="pcr-ctx-sep"></div>
    {#if scope !== "workflows"}
      <button class="pcr-ctx-item" onmouseenter={() => openSub = null} onclick={() => act("duplicates")}>Find duplicates</button>
    {/if}
    <button class="pcr-ctx-item" onmouseenter={() => openSub = null} onclick={() => act("refresh")}>Refresh</button>
    <button class="pcr-ctx-item" onmouseenter={() => openSub = null} onclick={() => act("properties")}>Properties</button>
  {/if}
</div>

<style>
  .pcr-ctx {
    position: fixed; z-index: 10000;
    min-width: 160px;
    background: rgba(38, 38, 38, 0.85);
    backdrop-filter: blur(20px) saturate(180%);
    border: 1px solid rgba(52, 52, 52, 0.6);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    padding: 4px 0;
  }
  .pcr-ctx-item {
    display: flex; align-items: center; gap: 10px;
    width: 100%; padding: 8px 12px;
    border: none; background: transparent;
    color: var(--input-text, #fff); font-size: 13px;
    text-align: left; cursor: pointer;
    transition: background-color 0.15s;
  }
  .pcr-ctx-item:hover { background: rgba(255, 255, 255, 0.1); }
  .pcr-ctx-item.active { color: #ff982a; }
  .pcr-ctx-has-sub { justify-content: flex-start; }
  .pcr-ctx-icon {
    width: 16px; height: 16px; flex-shrink: 0;
    color: var(--input-text, #aaa);
  }
  .pcr-ctx-chevron {
    margin-left: auto; font-size: 10px; opacity: 0.5;
  }
  .pcr-ctx-check {
    margin-left: auto; font-weight: bold; color: #ff982a;
  }
  .pcr-ctx-dir {
    margin-left: auto; font-size: 11px; opacity: 0.7;
  }
  .pcr-ctx-sep {
    height: 1px; margin: 4px 0;
    background: rgba(255, 255, 255, 0.08);
  }

  .pcr-ctx-sub-wrap { position: relative; }
  .pcr-ctx-sub {
    position: absolute; left: 100%; top: -4px;
    min-width: 130px;
    background: rgba(38, 38, 38, 0.85);
    backdrop-filter: blur(20px) saturate(180%);
    border: 1px solid rgba(52, 52, 52, 0.6);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    padding: 4px 0;
    z-index: 10001;
  }
</style>
