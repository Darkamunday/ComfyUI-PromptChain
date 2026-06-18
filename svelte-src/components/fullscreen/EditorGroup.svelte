<script>
  // One editor pane group. Owns a TabBar + CM6 mount point.
  // The bridge owns the CM6 editor lifecycle via overlayEl._pcrCreateEditorInGroup
  // (fired on mount) and _pcrDestroyEditorInGroup (on unmount). This component
  // just provides the mount element and hands ownership to the bridge.
  import { onMount } from "svelte";
  import TabBar from "./TabBar.svelte";
  import Menubar from "../node/Menubar.svelte";
  import WelcomePanel from "./WelcomePanel.svelte";

  let {
    group,
    isFocused = false,
    canClose = false,
    isDragActive = false,
    isExternalDrag = false,
    // isDragFromSelf: the tab being dragged came from THIS group.
    // sourceSingleTab: the dragging source group has exactly one tab.
    // Together they gate which drop zones render a preview — matches the
    // rejection rules in FullscreenEditor.handleTabDrop.
    isDragFromSelf = false,
    sourceSingleTab = false,
    treeRoots = [],
    logoTextUrl = "",
    overlayEl = null,
    onFocus = () => {},
    onSelectTab = () => {},
    onCloseTab = () => {},
    onReorderTabs = () => {},
    onTabDragStart = () => {},
    onTabDragEnd = () => {},
    onCloseGroup = () => {},
    onTabDrop = () => {},
  } = $props();

  const activeNode = $derived(
    group.activeTab && group.activeTab.type !== "wildcard" ? group.activeTab.node : null
  );
  const showWelcome = $derived(!group.activeTab);

  let mountEl;
  let activeZone = $state(null); // "center" | "top" | "right" | "bottom" | "left"

  onMount(() => {
    if (!overlayEl || !mountEl) return;
    overlayEl._pcrCreateEditorInGroup?.(group.id, mountEl, group.activeTab);
    return () => overlayEl?._pcrDestroyEditorInGroup?.(group.id);
  });

  // Map pointer position within the overlay rect to a drop zone. Uses a
  // 25% edge threshold on each side (matching VS Code's feel) — the center
  // zone is the inner 50% × 50% region; everything outside falls to the
  // nearest edge.
  function zoneFromPointer(rect, x, y) {
    const relX = (x - rect.left) / rect.width;
    const relY = (y - rect.top) / rect.height;
    const distLeft = relX;
    const distRight = 1 - relX;
    const distTop = relY;
    const distBottom = 1 - relY;
    const minDist = Math.min(distLeft, distRight, distTop, distBottom);
    if (minDist > 0.25) return "center";
    if (minDist === distRight) return "right";
    if (minDist === distBottom) return "bottom";
    if (minDist === distLeft) return "left";
    return "top";
  }

  // Returns true if dropping `zone` here would be rejected by
  // handleTabDrop. Mirrors the rules there so the preview doesn't flash
  // for invalid moves and the cursor shows "not-allowed".
  function isZoneInvalid(zone) {
    if (!isDragFromSelf) return false;
    if (sourceSingleTab) return true;   // self-drops on single-tab panes reject everywhere
    if (zone === "center") return true; // own center is a no-op even with many tabs
    return false;
  }

  function handleDragOver(e) {
    if (!isDragActive) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const zone = zoneFromPointer(rect, e.clientX, e.clientY);
    if (isZoneInvalid(zone)) {
      activeZone = null;
      return; // no preventDefault → drop not allowed, cursor shows not-allowed
    }
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    activeZone = zone;
  }
  function handleDragLeave() { activeZone = null; }
  function handleDrop(e) {
    if (!isDragActive) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const zone = zoneFromPointer(rect, e.clientX, e.clientY);
    if (isZoneInvalid(zone)) { activeZone = null; return; }
    e.preventDefault();
    activeZone = null;
    onTabDrop(group.id, zone);
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="pcr-fs-editor-column"
  class:pcr-fs-editor-column--focused={isFocused}
  data-group-id={group.id}
  style:flex-grow={group.flex ?? 1}
  onpointerdown={() => onFocus(group.id)}
>
  {#if group.tabs.length > 0}
    <TabBar
      tabs={group.tabs}
      activeTab={group.activeTab}
      {canClose}
      {isExternalDrag}
      {isFocused}
      onSelect={(tab) => onSelectTab(group.id, tab)}
      onClose={(tab) => onCloseTab(group.id, tab)}
      onReorder={(srcTab, targetTab, before) => onReorderTabs(group.id, srcTab, targetTab, before)}
      onDragStart={(tab) => onTabDragStart(group.id, tab)}
      onDragEnd={() => onTabDragEnd()}
      onCloseGroup={() => onCloseGroup(group.id)}
      onExternalDrop={() => onTabDrop(group.id, "center")}
    />
  {/if}

  <div class="pcr-fs-group-body">
    {#if showWelcome}
      <WelcomePanel {logoTextUrl} />
    {/if}
    <div
      class="pcr-fs-editor-frame"
      class:pcr-fs-editor-frame--locked={activeNode?._pcrShared?.locked && !activeNode?._pcrShared?.disabled}
      class:pcr-fs-editor-frame--disabled={!!activeNode?._pcrShared?.disabled}
      style:display={showWelcome ? "none" : ""}
    >
      <!-- Pane row: AI panel + divider on the LEFT (relocated by the
           bridge), Menubar + editor body in a column on the right. The
           AI panel pushes the Menubar's left edge in when open instead
           of spanning over it. -->
      <div class="pcr-fs-editor-pane-row">
        <div class="pcr-fs-editor-main-col">
          {#if activeNode && activeNode._pcrShared}
            <Menubar
              node={activeNode}
              shared={activeNode._pcrShared}
              inFullscreen={true}
              docDropdownEl={activeNode._pcrDocDropdownEl ?? null}
              onSetMode={(mode, switchIndex) => activeNode._pcrMenubar?.setMode?.(mode, switchIndex)}
              onToggleLock={() => activeNode._pcrMenubar?.toggleLock?.()}
              onToggleDisable={() => activeNode._pcrMenubar?.toggleDisable?.()}
              onToggleOutput={() => activeNode._pcrMenubar?.toggleOutput?.()}
              onToggleImage={() => activeNode._pcrMenubar?.toggleImage?.()}
              onToggleAssistant={() => activeNode._pcrMenubar?.toggleAssistant?.()}
              onResetIterate={() => activeNode._pcrMenubar?.resetIterate?.()}
            />
          {:else if group.activeTab?.type === "wildcard"}
            <div class="pcr-fs-wildcard-header">
              <span class="pcr-fs-wildcard-header-name">{group.activeTab.title}</span>
              <span class="pcr-fs-wildcard-header-hint">wildcard file</span>
            </div>
          {/if}
          <div bind:this={mountEl} class="pcr-fs-editor-body pcr-editor"></div>
        </div>
      </div>
    </div>

    <!-- Drop-zone overlay. Transparent to pointer events until a tab drag
         is active; then intercepts dragover/drop to show the snap preview. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="pcr-fs-drop-overlay"
      class:pcr-fs-drop-overlay--active={isDragActive}
      ondragover={handleDragOver}
      ondragleave={handleDragLeave}
      ondrop={handleDrop}
    >
      {#if activeZone}
        <div
          class="pcr-fs-drop-preview"
          class:pcr-fs-drop-preview--center={activeZone === "center"}
          class:pcr-fs-drop-preview--right={activeZone === "right"}
          class:pcr-fs-drop-preview--left={activeZone === "left"}
          class:pcr-fs-drop-preview--top={activeZone === "top"}
          class:pcr-fs-drop-preview--bottom={activeZone === "bottom"}
        ></div>
      {/if}
    </div>
  </div>
</div>

<style>
  .pcr-fs-editor-column {
    flex: 1 1 0;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
    background: var(--pcr-fs-editor-surface);
  }
  /* Pane separators are drawn by the splitters in LayoutNode — no
     directional border here so column-layout stacks don't get a stray
     vertical stripe on their left edge. */
  /* Group body holds the editor frame / welcome panel AND the absolutely
     positioned drop overlay. Positioning context lives here rather than on
     the column so tab bar isn't covered by the overlay. */
  .pcr-fs-group-body {
    flex: 1;
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-editor-frame {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  /* Solid tinted surface when the active node is locked or disabled.
     Same color is applied to the tab + menubar + editor body so the
     pane reads as one continuous state-tinted surface without the
     stripe-alignment issues across separate backgrounds. */
  .pcr-fs-editor-frame--locked .pcr-fs-editor-body :global(.cm-editor) {
    background: #251e0c !important;
  }
  .pcr-fs-editor-frame--disabled .pcr-fs-editor-body :global(.cm-editor) {
    background: #271111 !important;
  }
  .pcr-fs-editor-pane-row {
    flex: 1;
    display: flex;
    flex-direction: row;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-editor-main-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  /* AI panel + divider relocate here when the focused tab is this group's
     active node. Negative `order` floats them left of the main column
     (Menubar + editor body) without needing a custom prepend in the bridge. */
  .pcr-fs-editor-pane-row > :global(.pcr-ai-panel) {
    order: -2;
    flex-shrink: 1;
    background-color: transparent !important;
  }
  .pcr-fs-editor-pane-row > :global(.pcr-ai-divider) {
    order: -1;
    flex-shrink: 0;
    width: 4px;
    background-color: var(--pcr-fs-editor-surface);
    border-left: 1px solid #292929;
  }
  .pcr-fs-editor-pane-row > :global(.pcr-ai-divider):hover {
    background: rgba(255, 255, 255, 0.04);
  }
  /* Fullscreen tweaks for the composer surfaces — slightly tighter
     than node mode since the FS pane has more horizontal room.
     Header / composer drop their wash so the transparent panel column
     reads through. */
  .pcr-fs-editor-pane-row :global(.pcr-ai-panel-header) {
    background: transparent;
  }
  .pcr-fs-editor-pane-row :global(.pcr-ai-panel-composer) {
    background: transparent;
  }
  .pcr-fs-editor-pane-row :global(.pcr-ai-panel-input) {
    margin-top: 2px;
    margin-left: 2px;
    margin-right: 2px;
  }
  .pcr-fs-editor-pane-row :global(.pcr-ai-panel-composer-toolbar) {
    padding: 2px 7px 6px 6px;
  }
  .pcr-fs-editor-body {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .pcr-fs-editor-body :global(.cm-editor) {
    flex: 1;
    height: 100%;
    font-size: var(--pcr-font-size, 13px);
    border-radius: 0;
    background-color: var(--pcr-fs-editor-surface) !important;
  }
  .pcr-fs-editor-body :global(.cm-scroller) {
    overflow: auto;
  }
  .pcr-fs-editor-column > :global(.pcr-output-panel) { flex-shrink: 0; }
  .pcr-fs-editor-column > :global(.pcr-output-panel-resize) { flex-shrink: 0; }
  .pcr-fs-editor-column :global(.pcr-output-panel-header) {
    background: var(--pcr-fs-chrome-surface);
  }

  /* Lightweight header row shown above the editor for wildcard tabs.
     Node tabs get the full Menubar instead. */
  .pcr-fs-wildcard-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    height: 32px;
    background: rgb(0 0 0 / 60%);
    border-radius: 4px 4px 0 0;
    font-size: 13px;
    flex-shrink: 0;
  }
  .pcr-fs-wildcard-header-name {
    color: #E6DB74;
    font-style: italic;
  }
  .pcr-fs-wildcard-header-hint {
    color: rgba(255, 255, 255, 0.35);
    font-size: 11px;
  }

  /* ── Drop zone overlay ── */
  .pcr-fs-drop-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 10;
  }
  .pcr-fs-drop-overlay--active {
    pointer-events: auto;
  }
  .pcr-fs-drop-preview {
    position: absolute;
    background: rgba(79, 195, 247, 0.22);
    border: 1px solid rgba(79, 195, 247, 0.55);
    pointer-events: none;
    transition: inset 0.08s ease;
  }
  .pcr-fs-drop-preview--center { inset: 0; }
  .pcr-fs-drop-preview--right { inset: 0 0 0 50%; }
  .pcr-fs-drop-preview--left { inset: 0 50% 0 0; }
  .pcr-fs-drop-preview--top { inset: 0 0 50% 0; }
  .pcr-fs-drop-preview--bottom { inset: 50% 0 0 0; }
</style>
