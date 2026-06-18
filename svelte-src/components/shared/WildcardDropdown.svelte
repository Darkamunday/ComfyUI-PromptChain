<script module>
  const optionsCache = new Map();
</script>

<script>
  // WildcardDropdown — per-wildcard mode selector with async options list.
  // Renders directly at mount point (no PopupAnchor portal) since this component
  // is standalone-mounted on document.body by the bridge.

  import { onMount, onDestroy } from "svelte";
  import SearchableList from "./SearchableList.svelte";

  let {
    wildcardName,
    currentMode = "randomize",
    currentIndex = 0,
    triggerRect,
    popupKey = "",
    onSelectMode = () => {},
    onSelectOption = () => {},
    onClose = () => {},
  } = $props();

  let options = $state([]);
  let loading = $state(true);
  let loadError = $state(null);
  let searchList = $state(null);
  let menuEl;
  let openedAt = 0;
  let fetchAc = null;

  const modes = [
    { emoji: "\u{1F3B2}", label: "Randomize", value: "randomize" },
    { emoji: "\u{1F4DA}", label: "Combine", value: "combine" },
    { emoji: "\u267B\uFE0F", label: "Iterate", value: "iterate" },
    { emoji: "\u274C", label: "None", value: "none" },
  ];

  function selectMode(e, mode) {
    e.stopPropagation();
    onSelectMode(mode);
    requestAnimationFrame(() => onClose());
  }

  function selectOption(opt) {
    onSelectOption(opt);
    requestAnimationFrame(() => onClose());
  }

  function dismiss(e) {
    if (Date.now() - openedAt < 300) return;
    if (menuEl?.contains(e.target)) return;
    onClose();
  }

  function reposition() {
    if (!menuEl || !triggerRect) return;
    requestAnimationFrame(() => {
      if (!menuEl) return;
      const rect = menuEl.getBoundingClientRect();
      let left = triggerRect.left;
      let top = triggerRect.bottom + 4;
      if (left + rect.width > window.innerWidth) left = window.innerWidth - rect.width - 10;
      if (top + rect.height > window.innerHeight) top = triggerRect.top - rect.height - 4;
      if (left < 10) left = 10;
      if (top < 10) top = 10;
      menuEl.style.left = `${left}px`;
      menuEl.style.top = `${top}px`;
    });
  }

  onMount(() => {
    openedAt = Date.now();
    document.addEventListener("click", dismiss);
    document.addEventListener("pointerdown", dismiss);
    reposition();

    const cached = optionsCache.get(wildcardName);
    if (cached) {
      options = cached;
      loading = false;
      requestAnimationFrame(() => { reposition(); searchList?.focusSearch(); });
      return;
    }
    fetchAc = new AbortController();
    fetch(`/promptchain/wildcard?name=${encodeURIComponent(wildcardName)}&options=true`,
          { signal: fetchAc.signal })
      .then(async r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => {
        const raw = data.options || [];
        const mapped = raw.map((label, idx) => ({
          index: idx + 1,
          label: label.replace(/:\d+\.?\d*\)/g, ")").replace(/\s+/g, " ").trim(),
          fullLabel: label,
        }));
        optionsCache.set(wildcardName, mapped);
        options = mapped;
        loading = false;
        requestAnimationFrame(() => { reposition(); searchList?.focusSearch(); });
      })
      .catch(e => {
        if (e.name === "AbortError") return;
        console.error("[PromptChain] wildcard options load failed:", e);
        loadError = e.message || "Load failed";
        loading = false;
      });
  });

  onDestroy(() => {
    fetchAc?.abort();
    document.removeEventListener("click", dismiss);
    document.removeEventListener("pointerdown", dismiss);
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={menuEl}
  class="pcr-mode-menu"
  style="position:fixed;z-index:100000;"
  onclick={(e) => e.stopPropagation()}
  onpointerdown={(e) => e.stopPropagation()}
>
  <div class="pcr-mode-menu-modes">
    {#each modes as mode}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-mode-menu-item pcr-mode-menu-mode-option"
        class:pcr-mode-menu-selected={currentMode === mode.value}
        onclick={(e) => selectMode(e, mode.value)}
      >
        <span>{mode.emoji} {mode.label}</span>
        {#if currentMode === mode.value}
          <span class="pcr-mode-menu-check">{"\u2713"}</span>
        {/if}
      </div>
    {/each}
  </div>

  {#if loading}
    <div class="pcr-mode-menu-item" style="color:#888;font-style:italic;">
      Loading options{"\u2026"}
    </div>
  {:else if loadError}
    <div class="pcr-mode-menu-item" style="color:#e55;font-style:italic;">
      Error: {loadError}
    </div>
  {:else}
    <SearchableList
      bind:this={searchList}
      {options}
      onSelect={selectOption}
      currentMode={currentMode === "switch" ? "switch" : ""}
      currentSwitchIndex={currentIndex}
    />
  {/if}
</div>
