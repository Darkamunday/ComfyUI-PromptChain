<script>
  // ModeMenu — mode selector dropdown with searchable switch options.
  // Shows mode buttons (Randomize, Combine, Iterate, None) and a searchable
  // list of switch options (child nodes or inline labels).

  import PopupAnchor from "./PopupAnchor.svelte";
  import SearchableList from "./SearchableList.svelte";

  let {
    triggerRect,
    popupKey = "mode",
    currentMode = "switch",
    currentSwitchIndex = 1,
    switchOptions = [],
    hasMultipleOptions = false,
    onSelectMode = () => {},
    onSelectSwitch = () => {},
    onResetIterate = null,
    onClose = () => {},
  } = $props();

  let searchList;

  const modes = [
    { emoji: "\u{1F3B2}", label: "Randomize", value: "roll" },
    { emoji: "\u{1F4DA}", label: "Combine", value: "combine" },
    { emoji: "\u267B\uFE0F", label: "Iterate", value: "iterate" },
  ];

  function selectMode(e, mode) {
    e.stopPropagation();
    onSelectMode(mode);
    onClose();
  }

  function selectNone(e) {
    e.stopPropagation();
    onSelectSwitch({ index: 0 });
    onClose();
  }

  function selectSwitch(opt) {
    onSelectSwitch(opt);
    onClose();
  }

  function resetIterate(e) {
    e.stopPropagation();
    onResetIterate?.();
    onClose();
  }
</script>

<PopupAnchor {triggerRect} {popupKey} {onClose}>
  <div class="pcr-mode-menu-modes">
    {#each modes as mode}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-mode-menu-item pcr-mode-menu-mode-option"
        class:pcr-mode-menu-selected={currentMode === mode.value}
        class:pcr-mode-menu-disabled={!hasMultipleOptions}
        onclick={(e) => { if (hasMultipleOptions) selectMode(e, mode.value); }}
      >
        <span>{mode.emoji} {mode.label}</span>
        {#if currentMode === mode.value}
          <span class="pcr-mode-menu-check">{"\u2713"}</span>
        {/if}
        {#if mode.value === "iterate" && currentMode === "iterate" && onResetIterate}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <span class="pcr-mode-menu-reset" title="Reset iterate position" onclick={resetIterate}>{"\u21BA"}</span>
        {/if}
      </div>
    {/each}

    <!-- none option -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="pcr-mode-menu-item pcr-mode-menu-mode-option"
      class:pcr-mode-menu-selected={currentMode === "switch" && currentSwitchIndex === 0}
      onclick={selectNone}
    >
      <span>{"\u274C"} None</span>
      {#if currentMode === "switch" && currentSwitchIndex === 0}
        <span class="pcr-mode-menu-check">{"\u2713"}</span>
      {/if}
    </div>
  </div>

  <SearchableList
    bind:this={searchList}
    options={switchOptions}
    onSelect={selectSwitch}
    {currentMode}
    {currentSwitchIndex}
  />
</PopupAnchor>
