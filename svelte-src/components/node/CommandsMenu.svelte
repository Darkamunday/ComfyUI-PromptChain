<script>
  // CommandsMenu — composer toolbar `[/]` button. Opens a small popover
  // with toggles for opt-in features. Today: Extra verbs (Expand / Vary
  // / Condense / Reword / Enrich). Off by default — keeps the chat agent
  // system prompt lean for the simple-edit case so the 8B Qwen doesn't
  // get noisier from prompt bloat.

  import PopupAnchor from "../shared/PopupAnchor.svelte";

  let {
    extraVerbs = false,
    onChangeExtraVerbs = () => {},
  } = $props();

  let triggerEl = $state(null);
  let triggerRect = $state(null);
  let isOpen = $state(false);

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

  function close() { isOpen = false; }

  function toggleExtraVerbs(e) {
    e.stopPropagation();
    onChangeExtraVerbs(!extraVerbs);
  }
</script>

<button
  bind:this={triggerEl}
  type="button"
  class="pcr-ai-panel-tool-btn"
  class:pcr-ai-panel-tool-btn--active={extraVerbs}
  title="Commands & options"
  onclick={toggle}
>
  {@html '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="3"/><line x1="9" y1="15" x2="15" y2="9"/></svg>'}
</button>

{#if isOpen && triggerRect}
  <PopupAnchor {triggerRect} {triggerEl} popupKey="ai-commands" onClose={close}>
    <div class="pcr-ai-commands-menu">
      <div class="pcr-ai-commands-section-label">Options</div>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-mode-menu-item pcr-ai-commands-toggle"
        onclick={toggleExtraVerbs}
      >
        <div class="pcr-ai-commands-toggle-text">
          <span class="pcr-ai-commands-toggle-label">Extra verbs</span>
          <span class="pcr-ai-commands-toggle-sub">
            Expand / Vary / Condense / Reword / Enrich
          </span>
        </div>
        <span class="pcr-ai-commands-toggle-state" class:pcr-on={extraVerbs}>
          {extraVerbs ? "on" : "off"}
        </span>
      </div>
    </div>
  </PopupAnchor>
{/if}
