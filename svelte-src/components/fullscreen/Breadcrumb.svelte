<script>
  // Breadcrumb — path from root to active node in the tree.

  let {
    roots = [],
    activeNodeId = null,
    onNavigate = () => {},
  } = $props();

  function buildPath(tree, targetId) {
    function walk(node, path) {
      path.push(node);
      if (node.node.id === targetId) return [...path];
      for (const child of node.children) {
        const result = walk(child, path);
        if (result) return result;
      }
      path.pop();
      return null;
    }
    return tree ? walk(tree, []) || [] : [];
  }

  let path = $derived.by(() => {
    if (!activeNodeId || !roots.length) return [];
    for (const tree of roots) {
      const p = buildPath(tree, activeNodeId);
      if (p.length) return p;
    }
    return [];
  });
</script>

{#if path.length > 0}
  <div class="pcr-fs-breadcrumb">
    {#each path as item, i}
      {#if i > 0}
        <span class="pcr-fs-breadcrumb-sep">{"\u203A"}</span>
      {/if}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span
        class="pcr-fs-breadcrumb-item"
        class:pcr-fs-breadcrumb-active={i === path.length - 1}
        onclick={() => onNavigate(item)}
      >{item.title}</span>
    {/each}
  </div>
{/if}

<style>
  .pcr-fs-breadcrumb {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px 4px;
    font-size: 12px;
    color: #888;
    background: var(--pcr-fs-editor-surface);
    border-bottom: none;
    flex-shrink: 0;
    overflow-x: auto;
    white-space: nowrap;
  }
  .pcr-fs-breadcrumb::-webkit-scrollbar { height: 0; }
  .pcr-fs-breadcrumb-sep { color: #555; font-size: 14px; }
  .pcr-fs-breadcrumb-item {
    cursor: pointer;
    padding: 1px 4px;
    border-radius: 3px;
    transition: background 0.1s, color 0.1s;
  }
  .pcr-fs-breadcrumb-item:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #ccc;
  }
</style>
