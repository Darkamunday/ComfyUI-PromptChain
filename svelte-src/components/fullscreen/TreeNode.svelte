<script>
  import TreeNode from "./TreeNode.svelte";

  const MODE_EMOJI = { combine: "\u{1F4DA}", roll: "\u{1F3B2}", switch: "\u2705", iterate: "\u267B\uFE0F" };
  const MODE_COLORS = { combine: "#e99e2d", roll: "#da3e65", switch: "#73d952", iterate: "#33bdff" };

  let {
    tree,
    activeNodeId = null,
    renamingNodeId = null,
    onSelectNode = () => {},
    onSetMode = () => {},
    onToggleLock = () => {},
    onToggleDisable = () => {},
    onContextMenu = () => {},
    onLabelClick = () => {},
    onWildcardClick = () => {},
    onWildcardModeClick = () => {},
    onDragDrop = () => {},
    onFinishRename = () => {},
    refreshTree = () => {},
    isRoot = false,
    parentTree = null,
  } = $props();

  let isRenaming = $derived(renamingNodeId != null && tree.node.id === renamingNodeId);
  let renameValue = $state("");
  let renameInputEl = $state(null);
  let renameCommitted = false;

  $effect(() => {
    if (isRenaming) {
      renameValue = tree.title || "";
      renameCommitted = false;
      queueMicrotask(() => {
        renameInputEl?.focus();
        renameInputEl?.select();
      });
    }
  });

  function commitRename() {
    if (renameCommitted) return;
    renameCommitted = true;
    const val = renameValue.trim();
    onFinishRename(tree.node.id, val || null);
  }
  function cancelRename() {
    if (renameCommitted) return;
    renameCommitted = true;
    onFinishRename(tree.node.id, null);
  }

  let node = $derived(tree.node);
  // Read snapshotted properties from tree (new object each rebuild) instead of
  // node.properties (same LGraphNode ref — $derived won't detect mutations).
  let mode = $derived(tree.mode);
  let switchIndex = $derived(tree.switchIndex);
  let isInactive = $derived(tree._inactive);
  let isCollapsed = $derived(tree.collapsed);
  let isLocked = $derived(tree.locked);
  let isDisabled = $derived(tree.disabled);
  let isActive = $derived(activeNodeId != null && node.id === activeNodeId);

  let parentMode = $derived(parentTree?.mode || "switch");
  let parentSwitch = $derived(parentTree?.switchIndex ?? 1);
  let childIndex = $derived.by(() => {
    if (!parentTree?.children) return 0;
    const idx = parentTree.children.indexOf(tree);
    return idx >= 0 ? idx + 1 : 0;
  });
  let isSelected = $derived(parentMode === "switch" && parentSwitch === childIndex);

  function toggleCollapse(e) {
    e.stopPropagation();
    if (!node.properties) node.properties = {};
    node.properties.pcrTreeCollapsed = !node.properties.pcrTreeCollapsed;
    // force re-render — mutation on LGraphNode may not be tracked by Svelte proxy
    refreshTree();
  }

  function handleIndicatorClick(e) {
    e.stopPropagation();
    if (!parentTree?.node) return;
    if (parentMode === "switch") {
      // quick-select this child
      onSetMode(parentTree.node, "switch", childIndex);
    } else {
      // non-switch mode: open mode popup on the indicator element
      onSetMode(parentTree.node, parentMode, undefined, e.currentTarget);
    }
  }
</script>

<div class="pcr-nettree-item" class:pcr-nettree-item--inactive={isInactive} data-node-id={String(node.id)}>
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="pcr-nettree-row"
    class:pcr-nettree-row--root={isRoot}
    class:pcr-nettree-row--active={isActive}
    class:pcr-nettree-row--selected={!isRoot && isSelected}
    class:pcr-nettree-row--locked={isLocked}
    class:pcr-nettree-row--disabled={isDisabled}
    onpointerdown={(e) => e.stopPropagation()}
    onclick={() => onSelectNode(tree)}
    oncontextmenu={(e) => { e.preventDefault(); e.stopPropagation(); onContextMenu(node, tree, parentTree, e.clientX, e.clientY); }}
    draggable={true}
    ondragstart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", ""); e.currentTarget.classList.add("pcr-nettree-row--dragging"); e.currentTarget._dragTree = tree; e.currentTarget._dragParent = parentTree; }}
    ondragend={(e) => { e.currentTarget.classList.remove("pcr-nettree-row--dragging"); }}
    ondragover={(e) => {
      const src = e.currentTarget.closest(".pcr-nettree-items")?.querySelector(".pcr-nettree-row--dragging");
      if (!src || src._dragTree?.node === node) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const rect = e.currentTarget.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const third = rect.height / 3;
      e.currentTarget.closest(".pcr-nettree-items")?.querySelectorAll(".pcr-nettree-row").forEach(r => r.classList.remove("pcr-nettree-row--drop-before", "pcr-nettree-row--drop-after", "pcr-nettree-row--drop-onto"));
      if (y < third) e.currentTarget.classList.add("pcr-nettree-row--drop-before");
      else if (y > third * 2) e.currentTarget.classList.add("pcr-nettree-row--drop-after");
      else e.currentTarget.classList.add("pcr-nettree-row--drop-onto");
    }}
    ondragleave={(e) => { e.currentTarget.classList.remove("pcr-nettree-row--drop-before", "pcr-nettree-row--drop-after", "pcr-nettree-row--drop-onto"); }}
    ondrop={(e) => {
      e.preventDefault();
      const src = e.currentTarget.closest(".pcr-nettree-items")?.querySelector(".pcr-nettree-row--dragging");
      if (!src || src._dragTree?.node === node) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const third = rect.height / 3;
      const position = y < third ? "before" : y > third * 2 ? "after" : "into";
      const sameParent = src._dragParent?.node === parentTree?.node;
      // Drop-after on an expanded parent row means "the gap between parent and
      // its first child" — file-explorer convention is prepend-as-first-child,
      // not sibling-of-parent.
      if (position === "after" && tree.hasChildren && !isCollapsed) {
        onDragDrop("reparent", src._dragTree, tree, src._dragParent, parentTree, "first-child");
      } else if (position !== "into" && sameParent && !isRoot) {
        onDragDrop("reorder", src._dragTree, tree, src._dragParent, parentTree, position);
      } else {
        onDragDrop("reparent", src._dragTree, tree, src._dragParent, parentTree, position);
      }
    }}
  >
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <span
      class="pcr-nettree-toggle"
      class:pcr-nettree-toggle--empty={!tree.hasChildren}
      onclick={tree.hasChildren ? toggleCollapse : null}
    >
      {#if tree.hasChildren}
        {isCollapsed ? "\u25B6" : "\u25BC"}
      {/if}
    </span>

    {#if !isRoot && parentTree}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span
        class="pcr-nettree-indicator"
        class:pcr-nettree-indicator--unselected={parentMode === "switch" && !isSelected}
        class:pcr-nettree-indicator--combine={parentMode === "combine"}
        style:color={parentMode === "switch" ? (isSelected ? MODE_COLORS.switch : "") : (MODE_COLORS[parentMode] || "")}
        onpointerdown={(e) => e.stopPropagation()}
        onclick={handleIndicatorClick}
      >
        {#if parentMode === "switch"}
          {isSelected ? "\u2705" : "\u2610"}
        {:else}
          {MODE_EMOJI[parentMode] || ""}
        {/if}
      </span>
    {/if}

    {#if isRenaming}
      <input
        class="pcr-nettree-rename-input"
        type="text"
        bind:value={renameValue}
        bind:this={renameInputEl}
        onpointerdown={(e) => e.stopPropagation()}
        onclick={(e) => e.stopPropagation()}
        onblur={commitRename}
        onkeydown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") { e.preventDefault(); commitRename(); }
          else if (e.key === "Escape") { e.preventDefault(); cancelRename(); }
        }}
      />
    {:else}
      <span class="pcr-nettree-name">{tree.title}</span>
    {/if}

    <span class="pcr-nettree-actions">
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-nettree-action-btn" class:pcr-nettree-action--locked={isLocked}
        title={isLocked ? "Unlock" : "Lock"} onclick={(e) => { e.stopPropagation(); onToggleLock(node); }}>{"\u{1F512}"}</span>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-nettree-action-btn" class:pcr-nettree-action--disabled={isDisabled}
        title={isDisabled ? "Enable" : "Disable"} onclick={(e) => { e.stopPropagation(); onToggleDisable(node); }}>{"\u2298"}</span>
    </span>
  </div>

  {#if tree.hasChildren && !isCollapsed}
    <div class="pcr-nettree-children" class:pcr-nettree-children--root={isRoot}>
      {#each tree.children as child}
        <TreeNode
          tree={child}
          {activeNodeId}
          {renamingNodeId}
          {onSelectNode}
          {onSetMode}
          {onToggleLock}
          {onToggleDisable}
          {onContextMenu}
          {onLabelClick}
          {onWildcardClick}
          {onWildcardModeClick}
          {onDragDrop}
          {onFinishRename}
          {refreshTree}
          isRoot={false}
          parentTree={tree}
        />
      {/each}

      {#if tree.labels.length > 0}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pcr-nettree-item" class:pcr-nettree-item--inactive={isInactive}>
          <div class="pcr-nettree-row" class:pcr-nettree-row--selected={tree.labels.some(l => l.index === switchIndex)}
            onpointerdown={(e) => e.stopPropagation()}
            onclick={() => {
              // Always open the owning tab first — onLabelClick jumps the
              // cursor in whatever editor is currently active, so the tab
              // must be swapped before we dispatch the selection.
              onSelectNode(tree);
              const sel = tree.labels.find(l => l.index === switchIndex);
              if (sel) onLabelClick(node, sel.index);
            }}
            oncontextmenu={(e) => { e.preventDefault(); e.stopPropagation(); onContextMenu(node, tree, parentTree, e.clientX, e.clientY); }}
          >
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <span class="pcr-nettree-indicator"
              onpointerdown={(e) => e.stopPropagation()}
              onclick={(e) => { e.stopPropagation(); onLabelClick(node, null, e.currentTarget); }}
            >
              {#if tree.labels.some(l => l.index === switchIndex)}
                {"\u2705"}
              {:else if mode === "switch" && switchIndex === 0}
                {"\u274C"}
              {:else}
                {MODE_EMOJI[mode] || "\u{1F3B2}"}
              {/if}
            </span>
            <span class="pcr-nettree-toggle pcr-nettree-toggle--none"></span>
            <span class="pcr-nettree-name pcr-nettree-name--label">
              {#if tree.labels.some(l => l.index === switchIndex)}
                {tree.labels.find(l => l.index === switchIndex)?.label}
              {:else if mode === "switch" && switchIndex === 0}
                None
              {:else}
                {tree.labels.length} options
              {/if}
            </span>
          </div>
        </div>
      {/if}

      {#each tree.wildcards as wc}
        {@const keyName = wc.name.includes("/") ? wc.name.split("/").pop() : wc.name}
        {@const hasRolled = wc.mode === "randomize" && wc.rolledLabel}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pcr-nettree-item pcr-nettree-item--wildcard" class:pcr-nettree-item--inactive={isInactive}>
          <div class="pcr-nettree-row" onpointerdown={(e) => e.stopPropagation()}
            onclick={() => onSelectNode(tree, wc.name)}
            oncontextmenu={(e) => { e.preventDefault(); e.stopPropagation(); onSelectNode(tree, wc.name); }}>
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <span class="pcr-nettree-indicator pcr-nettree-indicator--wildcard"
              onpointerdown={(e) => e.stopPropagation()}
              onclick={(e) => { e.stopPropagation(); onWildcardModeClick(node, wc.name, e.currentTarget); }}
            >{wc.mode === "switch" && wc.index > 0 ? "\u2705" : MODE_EMOJI[wc.mode === "randomize" ? "roll" : wc.mode] || "\u{1F3B2}"}</span>
            <span class="pcr-nettree-toggle pcr-nettree-toggle--none"></span>
            <span class="pcr-nettree-name pcr-nettree-name--wildcard" title="__{wc.name}__"
              style:color={wc.mode === "switch" && wc.index > 0 ? MODE_COLORS.switch : wc.mode === "combine" ? MODE_COLORS.combine : wc.mode === "iterate" ? MODE_COLORS.iterate : wc.mode === "none" ? "#b0b0b0" : MODE_COLORS.roll}
            >
              {#if hasRolled}
                <span class="pcr-wc-rolled" class:pcr-wc-rolled--animate={hasRolled}>{wc.rolledLabel}</span>
              {:else if wc.mode === "switch" && wc.label}
                {keyName}: {wc.label}
              {:else if wc.mode === "combine"}
                {keyName} (all)
              {:else if wc.mode === "none"}
                {keyName} (off)
              {:else}
                {keyName}
              {/if}
            </span>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .pcr-nettree-item {
    padding: 0 4px;
    transition: opacity 0.15s;
  }
  .pcr-nettree-item--inactive {
    opacity: 0.45;
  }
  .pcr-nettree-item--inactive:hover {
    opacity: 0.7;
  }
  .pcr-nettree-row {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 5px 8px;
    cursor: pointer;
    border-radius: 4px;
    transition: background 0.1s;
    font-size: 13px;
    color: #e0e0e0;
  }
  .pcr-nettree-row:hover {
    background: rgba(255, 255, 255, 0.05);
  }
  .pcr-nettree-row--root {
    font-weight: 600;
    padding: 6px 8px;
  }
  .pcr-nettree-row--active {
    background: rgba(255, 255, 255, 0.08);
  }
  .pcr-nettree-row--locked {
    border-left: 2px solid #e99e2d;
    color: #e99e2d;
  }
  .pcr-nettree-row--disabled {
    border-left: 2px solid #e74c3c;
    color: #e74c3c;
  }
  .pcr-nettree-row--disabled .pcr-nettree-name {
    opacity: 0.4;
    text-decoration: line-through;
  }
  .pcr-nettree-actions {
    display: none;
    align-items: center;
    gap: 2px;
    margin-left: auto;
    flex-shrink: 0;
  }
  .pcr-nettree-row:hover .pcr-nettree-actions { display: flex; }
  .pcr-nettree-action-btn {
    font-size: 11px;
    cursor: pointer;
    opacity: 0.3;
    padding: 0 2px;
    transition: opacity 0.1s;
  }
  .pcr-nettree-action-btn:hover { opacity: 1; }
  .pcr-nettree-action--locked { opacity: 1; color: #e99e2d; }
  .pcr-nettree-action--disabled { opacity: 1; color: #e74c3c; }
  .pcr-nettree-row--selected .pcr-nettree-name {
    color: #fff;
  }
  .pcr-nettree-row--selected .pcr-nettree-name--label {
    color: #82ff59d4;
  }
  .pcr-nettree-indicator {
    width: 20px;
    flex-shrink: 0;
    text-align: center;
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
  }
  .pcr-nettree-indicator:hover {
    filter: brightness(1.3);
  }
  .pcr-nettree-indicator--root { font-size: 14px; }
  .pcr-nettree-indicator--unselected { color: #666; font-size: 14px; }
  .pcr-nettree-indicator--combine { font-size: 12px; }
  .pcr-nettree-toggle {
    width: 16px;
    height: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    color: #888;
    cursor: pointer;
    flex-shrink: 0;
    border-radius: 3px;
    transition: background 0.1s, color 0.1s;
  }
  .pcr-nettree-toggle:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #ccc;
  }
  .pcr-nettree-toggle--empty {
    cursor: default;
    visibility: hidden;
  }
  .pcr-nettree-toggle--none {
    display: none;
  }
  .pcr-nettree-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pcr-nettree-rename-input {
    flex: 1;
    min-width: 0;
    font: inherit;
    color: #fff;
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid #4fc3f7;
    border-radius: 3px;
    padding: 1px 5px;
    outline: none;
    box-sizing: border-box;
  }
  .pcr-nettree-name--label {
    font-weight: 400;
    color: #82ff59d4;
    font-style: normal;
    padding-left: 2px;
    cursor: pointer;
  }
  .pcr-nettree-name--label:hover {
    text-decoration: underline;
  }
  .pcr-nettree-name--wildcard {
    font-size: 12px;
    font-weight: 400;
    color: #82ff59d4;
    font-style: normal;
    padding-left: 2px;
    cursor: pointer;
  }
  .pcr-nettree-name--wildcard:hover {
    text-decoration: underline;
  }
  .pcr-nettree-indicator--wildcard {
    cursor: pointer;
  }
  .pcr-wc-rolled {
    display: inline-block;
  }
  @keyframes pcr-wc-roll {
    0% { opacity: 0; transform: translateY(8px); }
    30% { opacity: 1; transform: translateY(0); }
    100% { opacity: 1; transform: translateY(0); }
  }
  .pcr-wc-rolled--animate {
    animation: pcr-wc-roll 0.4s ease-out;
  }
  .pcr-nettree-children {
    margin-left: 18px;
    padding-left: 4px;
  }
  .pcr-nettree-children--root {
    margin-left: 8px;
    padding-left: 0;
  }
  /* tree connector line via per-item pseudo-elements */
  .pcr-nettree-children:not(.pcr-nettree-children--root) > :global(.pcr-nettree-item) {
    position: relative;
  }
  .pcr-nettree-children:not(.pcr-nettree-children--root) > :global(.pcr-nettree-item)::before {
    content: '';
    position: absolute;
    left: -4px;
    top: 0;
    bottom: 0;
    width: 1px;
    background: #333;
  }
  .pcr-nettree-children:not(.pcr-nettree-children--root) > :global(.pcr-nettree-item:last-child)::before {
    bottom: auto;
    height: 16px;
  }
  :global(.pcr-nettree-row--drop-before) { box-shadow: inset 0 2px 0 0 #4fc3f7; }
  :global(.pcr-nettree-row--drop-after) { box-shadow: inset 0 -2px 0 0 #4fc3f7; }
  :global(.pcr-nettree-row--drop-onto) {
    background: rgba(79, 195, 247, 0.15);
    outline: 1px solid rgba(79, 195, 247, 0.4);
  }
  :global(.pcr-nettree-row--dragging) { opacity: 0.4; }
</style>
