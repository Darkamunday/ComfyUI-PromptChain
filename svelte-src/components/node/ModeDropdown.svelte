<script>
  // ModeDropdown — composer toolbar control. Three modes:
  //   ask       — propose, wait for Accept/Reject
  //   auto      — apply each tool result immediately
  //   auto-run  — apply + queue prompt; toast on queue failure

  import PopupAnchor from "../shared/PopupAnchor.svelte";

  let {
    value = "ask",
    onChange = () => {},
  } = $props();

  const OPTIONS = [
    { value: "ask",      label: "Ask before edits",   subtitle: "Show changes for review" },
    { value: "auto",     label: "Edit Automatically", subtitle: "Apply changes immediately" },
    { value: "auto-run", label: "Auto Mode",          subtitle: "Apply + queue prompt" },
  ];

  let triggerEl = $state(null);
  let triggerRect = $state(null);
  let isOpen = $state(false);

  let activeOption = $derived(OPTIONS.find(o => o.value === value) || OPTIONS[0]);

  function toggle(e) {
    e.preventDefault();
    e.stopPropagation();
    if (isOpen) {
      isOpen = false;
      return;
    }
    triggerRect = triggerEl?.getBoundingClientRect() || null;
    isOpen = true;
  }

  function pick(e, opt) {
    e.stopPropagation();
    if (opt.value !== value) onChange(opt.value);
    isOpen = false;
  }

  function close() { isOpen = false; }
</script>

<button
  bind:this={triggerEl}
  type="button"
  class="pcr-ai-mode-dropdown-btn"
  title="Edit-confirm mode"
  onclick={toggle}
>
  <span class="pcr-ai-mode-dropdown-btn-label">{activeOption.label}</span>
  <span class="pcr-ai-mode-dropdown-btn-caret">{"▾"}</span>
</button>

{#if isOpen && triggerRect}
  <PopupAnchor {triggerRect} {triggerEl} popupKey="ai-mode" onClose={close}>
    <div class="pcr-ai-mode-menu">
      {#each OPTIONS as opt}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="pcr-mode-menu-item pcr-ai-mode-menu-row"
          class:pcr-mode-menu-selected={opt.value === value}
          onclick={(e) => pick(e, opt)}
        >
          <div class="pcr-ai-mode-menu-row-text">
            <span class="pcr-ai-mode-menu-row-label">{opt.label}</span>
            <span class="pcr-ai-mode-menu-row-sub">{opt.subtitle}</span>
          </div>
          {#if opt.value === value}
            <span class="pcr-mode-menu-check">{"✓"}</span>
          {/if}
        </div>
      {/each}
    </div>
  </PopupAnchor>
{/if}
