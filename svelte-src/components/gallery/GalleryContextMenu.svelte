<script>
  import { portal } from "../../lib/portal.js";

  let {
    x, y, targetItem, viewMode, selectionCount = 0,
    onAction, onClose,
  } = $props();

  let menuEl;
  let openSub = $state(null);

  let isItemMenu = $derived(!!targetItem);

  const VIEW_MODES = [
    { id: "justified", label: "Justified" },
    { id: "grid", label: "Grid" },
    { id: "list", label: "List" },
  ];

  function act(action) {
    onAction?.(action);
    onClose?.();
  }

  let pos = $derived((() => {
    const mw = 180, mh = 300;
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

<div use:portal class="pcr-ctx" bind:this={menuEl} style="left:{pos.left}px;top:{pos.top}px;">
  {#if isItemMenu}
    <button class="pcr-ctx-item" onclick={() => act("open")}>Open in Viewer</button>
    <div class="pcr-ctx-sep"></div>
    {#if selectionCount > 1}
      <button class="pcr-ctx-item" onclick={() => act("detach")}>Remove {selectionCount} from History</button>
      <button class="pcr-ctx-item pcr-ctx-danger" onclick={() => act("delete")}>Delete {selectionCount} Files</button>
    {:else}
      <button class="pcr-ctx-item" onclick={() => act("detach")}>Remove from History</button>
      <button class="pcr-ctx-item pcr-ctx-danger" onclick={() => act("delete")}>Delete File</button>
    {/if}
  {:else}
    <!-- view submenu -->
    <div class="pcr-ctx-sub-wrap"
      onmouseenter={() => openSub = "view"}
      onmouseleave={() => { if (openSub === "view") openSub = null; }}
    >
      <button class="pcr-ctx-item pcr-ctx-has-sub">
        <svg class="pcr-ctx-icon" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
        View
        <span class="pcr-ctx-chevron">&#9656;</span>
      </button>
      {#if openSub === "view"}
        <div class="pcr-ctx-sub">
          {#each VIEW_MODES as vm}
            <button class="pcr-ctx-item" class:active={viewMode === vm.id} onclick={() => act("view:" + vm.id)}>
              {vm.label}
              {#if viewMode === vm.id}<span class="pcr-ctx-check">&#10003;</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>

    <div class="pcr-ctx-sep"></div>
    <button class="pcr-ctx-item" onclick={() => act("refresh")}>Refresh</button>
    <button class="pcr-ctx-item pcr-ctx-danger" onclick={() => act("clear")}>Clear History</button>
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
  .pcr-ctx-danger { color: #e74c3c; }
  .pcr-ctx-danger:hover { background: rgba(231, 76, 60, 0.15); }
  .pcr-ctx-has-sub { justify-content: flex-start; }
  .pcr-ctx-icon {
    width: 16px; height: 16px; flex-shrink: 0;
    color: var(--input-text, #aaa);
  }
  .pcr-ctx-chevron { margin-left: auto; font-size: 10px; opacity: 0.5; }
  .pcr-ctx-check { margin-left: auto; font-weight: bold; color: #ff982a; }
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
