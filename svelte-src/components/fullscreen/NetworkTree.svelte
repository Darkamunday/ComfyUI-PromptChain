<script>
  import TreeNode from "./TreeNode.svelte";

  let {
    roots = [],
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
  } = $props();
</script>

{#each roots as tree}
  <TreeNode
    {tree}
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
    isRoot={true}
    parentTree={null}
  />
{/each}
