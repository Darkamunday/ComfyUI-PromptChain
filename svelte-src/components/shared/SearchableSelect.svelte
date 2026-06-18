<script>
  // SearchableSelect — a <select> stand-in for long model lists: the trigger
  // keeps the modal combo look, the dropdown adds the tag-builder-style
  // search filter (PopupAnchor body portal + filtered keyboard-navigable
  // list, same global pcr-mode-menu-* styles SearchableList uses).
  import { tick } from "svelte";
  import PopupAnchor from "./PopupAnchor.svelte";

  let {
    id = "",
    value = "",
    groups = [],          // [{ label, options: [{ value, label }] }] — empty label = ungrouped
    disabled = false,
    popupKey = "searchable-select",
    placeholder = "Search models...",
    onpick = () => {},
  } = $props();

  let open = $state(false);
  let triggerEl = $state(null);
  let triggerRect = $state(null);
  let searchEl = $state(null);
  let filter = $state("");
  let keyboardIndex = $state(-1);

  const allOptions = $derived(groups.flatMap((g) => g.options));
  const currentLabel = $derived(allOptions.find((o) => o.value === value)?.label || "");
  const terms = $derived(filter.toLowerCase().split(/\s+/).filter(Boolean));
  // Group labels count as match text too, so "sdxl" surfaces every checkpoint
  // under an SDXL group even when the filename doesn't carry the word.
  const filteredGroups = $derived(groups
    .map((g) => ({
      label: g.label,
      options: g.options.filter((o) =>
        terms.every((t) => o.label.toLowerCase().includes(t) || (g.label || "").toLowerCase().includes(t))),
    }))
    .filter((g) => g.options.length));
  const flat = $derived(filteredGroups.flatMap((g) => g.options));

  $effect(() => { filter; keyboardIndex = -1; });

  async function toggle() {
    if (disabled) return;
    if (open) { open = false; return; }
    triggerRect = triggerEl.getBoundingClientRect();
    filter = "";
    keyboardIndex = -1;
    open = true;
    await tick();
    searchEl?.focus();
  }

  function pick(opt) {
    open = false;
    if (opt.value !== value) onpick(opt.value);
  }

  function onSearchKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (flat.length) keyboardIndex = (keyboardIndex + 1) % flat.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length) keyboardIndex = keyboardIndex > 0 ? keyboardIndex - 1 : flat.length - 1;
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (keyboardIndex >= 0 && keyboardIndex < flat.length) pick(flat[keyboardIndex]);
      else if (flat.length === 1) pick(flat[0]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      open = false;
    }
    e.stopPropagation();
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlightText(text) {
    if (!terms.length) return escapeHtml(text);
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
</script>

<button
  {id}
  bind:this={triggerEl}
  type="button"
  class="pcr-ssel-trigger"
  {disabled}
  onclick={toggle}
>
  <span class="pcr-ssel-label">{currentLabel || "—"}</span>
  <span class="pcr-ssel-caret">{"▾"}</span>
</button>

{#if open}
  <PopupAnchor {triggerRect} {popupKey} {triggerEl} onClose={() => { open = false; }}>
    <div class="pcr-mode-menu-search-container">
      <input
        bind:this={searchEl}
        bind:value={filter}
        type="text"
        class="pcr-mode-menu-search"
        {placeholder}
        onkeydown={onSearchKeydown}
      />
    </div>
    <div class="pcr-mode-menu-separator"></div>
    <div class="pcr-mode-menu-list">
      {#if !flat.length}
        <div class="pcr-mode-menu-empty">No matching models</div>
      {:else}
        {#each filteredGroups as g}
          {#if g.label}
            <div class="pcr-ssel-group">{g.label}</div>
          {/if}
          {#each g.options as opt}
            {@const i = flat.indexOf(opt)}
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              class="pcr-mode-menu-item"
              class:pcr-mode-menu-selected={opt.value === value}
              class:pcr-mode-menu-keyboard-selected={i === keyboardIndex}
              onclick={(e) => { e.stopPropagation(); pick(opt); }}
              onmouseenter={() => { keyboardIndex = i; }}
            >
              <span class="pcr-mode-menu-label">{@html highlightText(opt.label)}</span>
              {#if opt.value === value}
                <span class="pcr-mode-menu-check">{"✓"}</span>
              {/if}
            </div>
          {/each}
        {/each}
      {/if}
    </div>
  </PopupAnchor>
{/if}

<style>
  /* mirrors .pcr-up-select / .pcr-ip-select — the native combos this replaces */
  .pcr-ssel-trigger {
    width: 100%; box-sizing: border-box;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 5px 8px; font-size: 12px; color: #999;
    background: #1c1c1c; border: 1px solid #3a3a3a; border-radius: 5px;
    outline: none; cursor: pointer; font-family: inherit; text-align: left;
  }
  .pcr-ssel-trigger:disabled { opacity: 0.55; cursor: default; }
  .pcr-ssel-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pcr-ssel-caret { flex: none; color: #777; font-size: 10px; }
  .pcr-ssel-group {
    padding: 6px 10px 3px; font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px; color: #6b6b75;
    user-select: none;
  }
</style>
