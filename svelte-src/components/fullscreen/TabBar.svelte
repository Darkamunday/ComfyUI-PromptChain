<script>
  let {
    tabs = [],
    activeTab = null,
    canClose = false,
    isExternalDrag = false,
    isFocused = true,
    onSelect = () => {},
    onClose = () => {},
    onReorder = () => {},
    onCloseGroup = () => {},
    onDragStart = () => {},
    onDragEnd = () => {},
    onExternalDrop = () => {},
  } = $props();

  // Compare by stable underlying identity (LGraphNode / wildcard filename)
  // rather than strict-eq on the tab object. Svelte 5 can return different
  // proxies for tabs[i] vs activeTab even when they wrap the same source,
  // which silently breaks the active-class detection and the CSS trick
  // that blends the active tab's bottom border into the editor surface.
  function isActive(tab, active) {
    if (!active || !tab) return false;
    if (tab === active) return true;
    if (tab.type === "wildcard") {
      return active.type === "wildcard" && tab.filename === active.filename;
    }
    return !!tab.node && tab.node === active.node;
  }

  // The dragged tab lives in whichever pane started the drag — for
  // cross-pane inserts we need to find it anywhere in the overlay, not
  // just inside this tab bar.
  function findDraggingEl() { return document.querySelector(".pcr-fs-tab-dragging"); }
</script>

<div
  class="pcr-fs-tabbar"
  class:pcr-fs-tabbar--unfocused={!isFocused}
>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="pcr-fs-tabs"
    ondragover={(e) => {
      const src = findDraggingEl();
      if (!src) return;
      // Individual tabs handle their own drop-before/drop-after indicator
      // when the cursor is over them — only style the empty area past the
      // last tab here (shows drop-after on the last tab).
      if (e.target.closest(".pcr-fs-tab")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const allTabs = e.currentTarget.querySelectorAll(".pcr-fs-tab");
      allTabs.forEach(t => t.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after"));
      const lastTab = allTabs[allTabs.length - 1];
      if (lastTab && lastTab !== src) {
        lastTab.classList.add("pcr-fs-tab-drop-after");
      }
    }}
    ondragleave={(e) => {
      // Only clear when the pointer exits the container entirely (not
      // when moving between children — that's handled by each tab).
      if (!e.relatedTarget || !e.currentTarget.contains(e.relatedTarget)) {
        e.currentTarget.querySelectorAll(".pcr-fs-tab").forEach(t =>
          t.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after")
        );
      }
    }}
    ondrop={(e) => {
      const src = findDraggingEl();
      if (!src || !src._dragTab) return;
      e.preventDefault();
      e.currentTarget.querySelectorAll(".pcr-fs-tab").forEach(t =>
        t.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after")
      );
      // Drop in the empty area past the last tab: append to end of this
      // pane's tab list. Works for both same-pane (reorder) and
      // cross-pane (move) — handleTabReorder detects which case it is.
      if (isExternalDrag) { onExternalDrop(); return; }
      if (tabs.length < 2) return;
      onReorder(src._dragTab, tabs[tabs.length - 1], false);
    }}>
    {#each tabs as tab, i}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="pcr-fs-tab"
        class:pcr-fs-tab-active={isActive(tab, activeTab)}
        class:pcr-fs-tab--wildcard={tab.type === "wildcard"}
        class:pcr-fs-tab--locked={tab.node?._pcrShared?.locked && !tab.node?._pcrShared?.disabled}
        class:pcr-fs-tab--disabled={!!tab.node?._pcrShared?.disabled}
        onclick={() => onSelect(tab)}
        onauxclick={(e) => { if (e.button === 1) { e.preventDefault(); onClose(tab); } }}
        draggable={true}
        ondragstart={(e) => {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", "");
          e.currentTarget.classList.add("pcr-fs-tab-dragging");
          e.currentTarget._dragTab = tab;
          // Match VS Code: grabbing an inactive tab activates it
          // immediately so its content shows while being dragged.
          onSelect(tab);
          onDragStart(tab);
        }}
        ondragend={(e) => {
          e.currentTarget.classList.remove("pcr-fs-tab-dragging");
          e.currentTarget.closest(".pcr-fs-tabs")?.querySelectorAll(".pcr-fs-tab").forEach(t => t.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after"));
          onDragEnd();
          // Re-select the *originally dragged* tab, not the closure's
          // `tab`. Index-keyed {#each} rebinds that closure to whatever
          // now sits at this index after a reorder — so after moving
          // Quality from slot 2 to slot 1, the dragend handler on slot 2
          // sees `tab === Poses` and would activate Poses by mistake.
          onSelect(e.currentTarget._dragTab ?? tab);
        }}
        ondragover={(e) => {
          const src = findDraggingEl();
          if (!src || src._dragTab === tab) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          const rect = e.currentTarget.getBoundingClientRect();
          const before = e.clientX < rect.left + rect.width / 2;
          e.currentTarget.closest(".pcr-fs-tabs")?.querySelectorAll(".pcr-fs-tab").forEach(t => t.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after"));
          e.currentTarget.classList.add(before ? "pcr-fs-tab-drop-before" : "pcr-fs-tab-drop-after");
        }}
        ondragleave={(e) => { e.currentTarget.classList.remove("pcr-fs-tab-drop-before", "pcr-fs-tab-drop-after"); }}
        ondrop={(e) => {
          e.preventDefault();
          // Stop bubbling — otherwise the container's ondrop runs next
          // and unconditionally moves the tab to the end, undoing the
          // insert position we just computed here.
          e.stopPropagation();
          const src = findDraggingEl();
          if (!src || src._dragTab === tab) return;
          const rect = e.currentTarget.getBoundingClientRect();
          const before = e.clientX < rect.left + rect.width / 2;
          // Same callback for same-pane reorder and cross-pane insert;
          // handleTabReorder in FullscreenEditor detects the case.
          onReorder(src._dragTab, tab, before);
        }}
      >
        <span class="pcr-fs-tab-title">{tab.title}</span>
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <span class="pcr-fs-tab-close" onclick={(e) => { e.stopPropagation(); onClose(tab); }}>{"\u00D7"}</span>
      </div>
    {/each}
  </div>
  <div class="pcr-fs-tabbar-actions">
    {#if canClose}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-fs-group-btn" title="Close group" onclick={(e) => { e.stopPropagation(); onCloseGroup(); }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="5" y1="5" x2="19" y2="19"/><line x1="19" y1="5" x2="5" y2="19"/></svg>
      </span>
    {/if}
  </div>
</div>

<style>
  .pcr-fs-tabbar {
    display: flex;
    align-items: stretch;
    background: var(--pcr-fs-chrome-surface);
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-tabs {
    display: flex;
    flex: 1;
    overflow-x: auto;
    min-width: 0;
    /* Gradient (not a plain border) so the 1px divider only appears in
       the empty space after the last tab. Individual tabs' own
       border-bottom covers their region; the active tab's border is
       editor-surface-colored so it blends into the editor below. A
       plain border-bottom here would draw a seam under the active tab. */
    background: linear-gradient(to top, #3c3c3c 1px, var(--pcr-fs-chrome-surface) 1px);
  }
  .pcr-fs-tabs::-webkit-scrollbar { height: 0; }
  .pcr-fs-tab {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 6px 10px 16px;
    cursor: pointer;
    white-space: nowrap;
    font-size: 13px;
    color: #888;
    border-top: 2px solid transparent;
    border-bottom: 1px solid #3c3c3c;
    border-left: 1px solid transparent;
    border-right: 1px solid #252525;
    background: var(--pcr-fs-chrome-surface);
    user-select: none;
    transition: color 0.1s;
  }
  .pcr-fs-tab:not(.pcr-fs-tab-active):hover { color: #ccc; background: var(--pcr-fs-editor-surface); }
  .pcr-fs-tab-active {
    color: #fff;
    background: var(--pcr-fs-editor-surface);
    border-top-color: #4fc3f7;
    border-left: 1px solid #3c3c3c;
    border-right: 1px solid #3c3c3c;
    border-bottom-color: var(--pcr-fs-editor-surface);
  }
  .pcr-fs-tab-active:first-child { border-left-color: transparent; }
  .pcr-fs-tab--wildcard .pcr-fs-tab-title { color: #E6DB74; font-style: italic; }
  .pcr-fs-tab--wildcard.pcr-fs-tab-active { border-top-color: #E6DB74; }
  /* Locked / disabled state indicators — colored top accent, visible even
     when the tab isn't focused so you can see state across all panes.
     Disabled overrides locked (matches handleLockCascade semantics). */
  .pcr-fs-tab--locked { border-top-color: rgba(210, 177, 21, 0.45); }
  .pcr-fs-tab--locked.pcr-fs-tab-active { border-top-color: #d2b115; }
  .pcr-fs-tab--disabled { border-top-color: rgba(196, 32, 32, 0.5); }
  .pcr-fs-tab--disabled.pcr-fs-tab-active { border-top-color: #c42020; }
  /* Solid tinted surface — both active and inactive tabs in a locked or
     disabled state get the tint so the whole pane's tab row reads as
     state-aware at a glance. Bottom-border also switches to the tint on
     the active tab so there's no 1px seam against the tinted menubar
     below. Hover override prevents the inactive :hover rule above from
     reverting the tint back to editor-surface. */
  .pcr-fs-tab--locked { background: #251e0c; }
  .pcr-fs-tab--disabled { background: #271111; }
  .pcr-fs-tab--locked.pcr-fs-tab-active { border-bottom-color: #251e0c; }
  .pcr-fs-tab--disabled.pcr-fs-tab-active { border-bottom-color: #271111; }
  .pcr-fs-tab--locked:not(.pcr-fs-tab-active):hover { background: #2e2614; }
  .pcr-fs-tab--disabled:not(.pcr-fs-tab-active):hover { background: #321919; }
  :global(.pcr-fs-tab-drop-before) { box-shadow: inset 2px 0 0 0 #d4d4d4; }
  :global(.pcr-fs-tab-drop-after) { box-shadow: inset -2px 0 0 0 #d4d4d4; }
  .pcr-fs-tab-title { line-height: 1px; }
  .pcr-fs-tab-close {
    font-size: 19px;
    line-height: 1;
    margin-top: -3px;
    color: transparent;
    padding: 1px 3px;
    border-radius: 3px;
    transition: color 0.1s, background 0.1s;
  }
  .pcr-fs-tab:hover .pcr-fs-tab-close,
  .pcr-fs-tab-active .pcr-fs-tab-close { color: #888; }
  .pcr-fs-tab-close:hover {
    color: #fff !important;
    background: rgba(255, 255, 255, 0.1);
  }
  /* The cyan active-tab accent is a focus indicator, so only the
     focused pane's active tab should show it. Unfocused panes' active
     tabs keep the white text + surface color but drop the top stripe
     (matches VS Code's split-view). !important overrides the
     wildcard / locked / disabled top-border-color rules. */
  .pcr-fs-tabbar--unfocused .pcr-fs-tab-active {
    border-top-color: transparent !important;
    color: #888;
  }
  /* Group-level actions (split, close-group) sit to the right of the tabs.
     Transparent background matches the tab bar's gradient so the 1px
     divider at the bottom of the container shows through consistently
     with the empty-after-tabs region. */
  .pcr-fs-tabbar-actions {
    display: flex;
    align-items: center;
    padding: 0 6px;
    gap: 2px;
    border-bottom: 1px solid #3c3c3c;
    background: var(--pcr-fs-chrome-surface);
    flex-shrink: 0;
  }
  .pcr-fs-group-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    color: #888;
    border-radius: 3px;
    cursor: pointer;
    transition: color 0.1s, background 0.1s;
  }
  .pcr-fs-group-btn:hover {
    color: #fff;
    background: rgba(255, 255, 255, 0.08);
  }
</style>
