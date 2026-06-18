<script>
  // Recursive layout node. Containers render a row/column flex with
  // splitters between adjacent children; leaves render an EditorGroup.
  // FullscreenEditor owns the tree; we just pass props down.
  import EditorGroup from "./EditorGroup.svelte";
  import LayoutNode from "./LayoutNode.svelte";

  let {
    node,
    overlayEl,
    focusedLeafId,
    draggingTab,
    dragSourceSingleTab,
    treeRoots,
    logoTextUrl,
    leafCount,
    onFocus,
    onSelectTab,
    onCloseTab,
    onReorderTabs,
    onTabDragStart,
    onTabDragEnd,
    onTabDrop,
    onCloseGroup,
    onStartResize,
  } = $props();
</script>

{#if node.kind === "leaf"}
  <EditorGroup
    group={node}
    {overlayEl}
    isFocused={node.id === focusedLeafId}
    canClose={leafCount > 1}
    isDragActive={!!draggingTab}
    isExternalDrag={!!draggingTab && draggingTab.groupId !== node.id}
    isDragFromSelf={!!draggingTab && draggingTab.groupId === node.id}
    sourceSingleTab={dragSourceSingleTab}
    {treeRoots}
    {logoTextUrl}
    {onFocus}
    {onSelectTab}
    {onCloseTab}
    {onReorderTabs}
    {onTabDragStart}
    {onTabDragEnd}
    {onTabDrop}
    {onCloseGroup}
  />
{:else}
  <div
    class="pcr-fs-layout-container"
    class:pcr-fs-layout-container--column={node.direction === "column"}
    style:flex-grow={node.flex ?? 1}
  >
    {#each node.children as child, i (child.id)}
      {#if i > 0}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="pcr-fs-pane-splitter"
          class:pcr-fs-pane-splitter--vertical={node.direction === "column"}
          onpointerdown={(e) => onStartResize(node, i - 1, i, e)}
        ></div>
      {/if}
      <LayoutNode
        node={child}
        {overlayEl}
        {focusedLeafId}
        {draggingTab}
        {dragSourceSingleTab}
        {treeRoots}
        {logoTextUrl}
        {leafCount}
        {onFocus}
        {onSelectTab}
        {onCloseTab}
        {onReorderTabs}
        {onTabDragStart}
        {onTabDragEnd}
        {onTabDrop}
        {onCloseGroup}
        {onStartResize}
      />
    {/each}
  </div>
{/if}

<style>
  .pcr-fs-layout-container {
    flex: 1 1 0;
    display: flex;
    flex-direction: row;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-layout-container--column {
    flex-direction: column;
  }
  /* 4px drag target at every nesting depth, matching the sidebar resize
     handle. Row containers get a col-resize splitter; column containers
     flip the axis. */
  .pcr-fs-pane-splitter {
    flex-shrink: 0;
    width: 4px;
    cursor: col-resize;
    background: #252525;
    transition: background 0.1s;
  }
  .pcr-fs-pane-splitter:hover { background: rgba(79, 195, 247, 0.4); }
  .pcr-fs-pane-splitter:active { background: rgba(79, 195, 247, 0.8); }
  .pcr-fs-pane-splitter--vertical {
    width: auto;
    height: 4px;
    cursor: row-resize;
  }
</style>
