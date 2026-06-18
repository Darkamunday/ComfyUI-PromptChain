<script>
  // FullscreenEditor — IDE-like overlay. Svelte handles layout, bridge handles
  // CM6 editor, panel relocation, footer relocation, and execution tracking.

  import { onMount } from "svelte";
  import Topbar from "./Topbar.svelte";
  import NetworkTree from "./NetworkTree.svelte";
  import LayoutNode from "./LayoutNode.svelte";

  let {
    fsState,
    overlayEl = null,
    refreshTree = () => {},
    entryNodeId = null,
    onAddNode = () => {},
    onSelectNode = () => {},
    onSetMode = () => {},
    onToggleLock = () => {},
    onToggleDisable = () => {},
    onContextMenu = () => {},
    onEmptyContextMenu = () => {},
    onLabelClick = () => {},
    onWildcardClick = () => {},
    onWildcardModeClick = () => {},
    onDragDrop = () => {},
    onFinishRename = () => {},
    onClose = () => {},
    onQueuePrompt = () => {},
    onCancelExecution = () => {},
    onSaveWorkflow = () => {},
    logoUrl = "",
    logoTextUrl = "",
    workflowName = "Workflow",
    onComfyCommand = () => {},
  } = $props();

  // Live copy of the title so the bridge can update it after a rename
  // (Save As on a temporary workflow repaths in place without a reload).
  let liveWorkflowName = $state(workflowName);

  let sidebarWidth = $state(250);
  // Activity-bar state. VS Code semantics: clicking the active icon
  // collapses the content column; clicking any icon while collapsed
  // reopens with that view. Initial values come from fsState so the
  // fullscreen bridge can seed from persisted root.properties.
  let activeView = $state(fsState?.initialSidebarView ?? "edit");
  let sidebarCollapsed = $state(!!fsState?.initialSidebarCollapsed);

  // Mirror of ComfyUI's sidebar logo menu. The real PrimeVue popup renders below
  // this overlay's z-index, so we render our own dropdown and fire the same
  // Comfy commands the host menu does.
  let comfyMenuOpen = $state(false);
  let comfyIconEl;
  let comfyMenuPos = $state({ left: 0, top: 0 });
  function toggleComfyMenu() {
    if (!comfyMenuOpen && comfyIconEl) {
      const r = comfyIconEl.getBoundingClientRect();
      comfyMenuPos = { left: r.right + 4, top: r.top };
    }
    comfyMenuOpen = !comfyMenuOpen;
  }
  const COMFY_MENU_ITEMS = [
    { label: "New", command: "Comfy.NewBlankWorkflow" },
    { sep: true },
    { label: "Open…", command: "Comfy.OpenWorkflow" },
    { label: "Save", command: "Comfy.SaveWorkflow" },
    { label: "Save As…", command: "Comfy.SaveWorkflowAs" },
    { label: "Export", command: "Comfy.ExportWorkflow" },
    { label: "Export (API)", command: "Comfy.ExportWorkflowAPI" },
    { sep: true },
    { label: "Settings", command: "Comfy.ShowSettingsDialog" },
  ];
  function runComfyCommand(commandId) {
    comfyMenuOpen = false;
    // Routed through the bridge so editor text + pane layout are flushed
    // and captured before the command serializes or reloads the graph.
    onComfyCommand(commandId);
  }

  // Pane groups. Each group owns its own tab list and active tab.
  // `focusedGroupId` tracks which group receives tree clicks, keyboard
  // shortcuts, wildcard-tab insertions, etc.
  function findEntryTab() {
    const roots = fsState?.treeRoots || [];
    if (!roots.length) return null;
    if (entryNodeId) {
      function findInTree(tree, visited) {
        if (visited.has(tree)) return null;
        visited.add(tree);
        if (tree.node.id === entryNodeId) return { node: tree.node, title: tree.title };
        for (const child of tree.children) {
          const found = findInTree(child, visited);
          if (found) return found;
        }
        return null;
      }
      for (const root of roots) {
        const found = findInTree(root, new Set());
        if (found) return found;
      }
    }
    return { node: roots[0].node, title: roots[0].title };
  }
  const initialTab = findEntryTab();
  let nextGroupId = 1;

  // Layout tree. Every node is either:
  //   - leaf:      { kind: "leaf", id, tabs, activeTab, flex }
  //   - container: { kind: "container", id, direction: "row"|"column", children, flex }
  // Root is always a container so splits in any direction are uniform —
  // split a leaf matching the parent's direction → insert sibling;
  // split in the perpendicular direction → wrap the leaf in a new
  // container of that direction. Matches VS Code's grid model.
  function hydrateLayoutNode(raw) {
    if (!raw) return null;
    if (raw.kind === "container") {
      const children = (raw.children || []).map(hydrateLayoutNode).filter(Boolean);
      if (children.length === 0) return null;
      return {
        kind: "container",
        id: nextGroupId++,
        direction: raw.direction === "column" ? "column" : "row",
        flex: typeof raw.flex === "number" && raw.flex > 0 ? raw.flex : 1,
        children,
      };
    }
    // leaf (explicit kind or legacy tabs-without-kind)
    const tabs = [...(raw.tabs || [])];
    if (tabs.length === 0) return null;
    return {
      kind: "leaf",
      id: nextGroupId++,
      tabs,
      activeTab: tabs[raw.activeTabIdx] ?? tabs[0] ?? null,
      flex: typeof raw.flex === "number" && raw.flex > 0 ? raw.flex : 1,
    };
  }

  // Prefer a restored pane layout from the previous fullscreen session.
  // New format (tree) comes in via fsState.initialLayout; legacy flat
  // format via fsState.initialGroups. Fall back to a default single pane
  // with the entry tab.
  function buildInitialLayout() {
    const savedTree = fsState?.initialLayout;
    if (savedTree) {
      const hydrated = hydrateLayoutNode(savedTree);
      if (hydrated) {
        if (hydrated.kind === "container") return hydrated;
        // Root must be a container; wrap a lone leaf.
        return {
          kind: "container",
          id: nextGroupId++,
          direction: "row",
          flex: 1,
          children: [hydrated],
        };
      }
    }
    const legacyFlat = fsState?.initialGroups;
    if (legacyFlat && legacyFlat.length > 0) {
      return {
        kind: "container",
        id: nextGroupId++,
        direction: "row",
        flex: 1,
        children: legacyFlat.map(sg => {
          const tabs = [...sg.tabs];
          return {
            kind: "leaf",
            id: nextGroupId++,
            tabs,
            activeTab: tabs[sg.activeTabIdx] ?? tabs[0] ?? null,
            flex: typeof sg.flex === "number" && sg.flex > 0 ? sg.flex : 1,
          };
        }),
      };
    }
    return {
      kind: "container",
      id: nextGroupId++,
      direction: "row",
      flex: 1,
      children: [{
        kind: "leaf",
        id: nextGroupId++,
        tabs: initialTab ? [initialTab] : [],
        activeTab: initialTab,
        flex: 1,
      }],
    };
  }

  let rootLayout = $state(buildInitialLayout());

  // Tree helpers — source of truth queries against rootLayout.
  function allLeaves(node) {
    if (!node) return [];
    if (node.kind === "leaf") return [node];
    return node.children.flatMap(allLeaves);
  }
  function findLeaf(id, node = rootLayout) {
    if (!node) return null;
    if (node.kind === "leaf") return node.id === id ? node : null;
    for (const c of node.children) {
      const f = findLeaf(id, c);
      if (f) return f;
    }
    return null;
  }
  function findFirstLeaf(node = rootLayout) {
    if (!node) return null;
    if (node.kind === "leaf") return node;
    for (const c of node.children) {
      const f = findFirstLeaf(c);
      if (f) return f;
    }
    return null;
  }
  // Find the container that directly holds nodeWithId (leaf or container).
  function findParentContainer(childId, current = rootLayout) {
    if (!current || current.kind === "leaf") return null;
    for (const c of current.children) {
      if (c.id === childId) return current;
      if (c.kind === "container") {
        const res = findParentContainer(childId, c);
        if (res) return res;
      }
    }
    return null;
  }

  // Work out the initial focused leaf id across legacy + new persistence.
  const initialFocusedLeafId = (() => {
    const saved = fsState?.initialFocusedLeafId;
    if (saved != null && findLeaf(saved)) return saved;
    if (typeof fsState?.initialFocusedGroupIdx === "number") {
      const leaves = allLeaves(rootLayout);
      const idx = Math.max(0, Math.min(leaves.length - 1, fsState.initialFocusedGroupIdx));
      if (leaves[idx]) return leaves[idx].id;
    }
    return findFirstLeaf(rootLayout)?.id;
  })();
  let focusedGroupId = $state(initialFocusedLeafId);

  // The user invoked maximize from `entryNode` — they expect that node's
  // tab to be the active one when fullscreen opens, regardless of which
  // tab was active when fullscreen last closed. If a saved layout was
  // restored that doesn't contain the entry node, drop a fresh tab into
  // the leaf that was about to receive focus. If it's already in the
  // layout, just bring it forward.
  if (initialTab?.node && entryNodeId) {
    let targetLeaf = null;
    let entryTab = null;
    for (const leaf of allLeaves(rootLayout)) {
      const found = leaf.tabs.find(t => t.node && t.node.id === entryNodeId);
      if (found) { targetLeaf = leaf; entryTab = found; break; }
    }
    if (!targetLeaf) {
      targetLeaf = findLeaf(initialFocusedLeafId) || findFirstLeaf(rootLayout);
      if (targetLeaf) {
        targetLeaf.tabs = [...targetLeaf.tabs, initialTab];
        entryTab = targetLeaf.tabs[targetLeaf.tabs.length - 1];
      }
    }
    if (targetLeaf && entryTab) {
      targetLeaf.activeTab = entryTab;
      focusedGroupId = targetLeaf.id;
    }
  }

  // Track which tab is currently being dragged so every EditorGroup can
  // show its drop-zone overlay. Set by TabBar's ondragstart, cleared by
  // ondragend. Contents: { groupId, tab } or null.
  let draggingTab = $state(null);
  // True when the currently-dragging tab comes from a pane with only one
  // tab. In that case the source pane can't be split into itself (the
  // reject rule in handleTabDrop) so the overlay suppresses previews on
  // the source pane. Same for every pane's overlay since the check is
  // source-only.
  const dragSourceSingleTab = $derived.by(() => {
    if (!draggingTab) return false;
    const srcL = findLeaf(draggingTab.groupId);
    return !!srcL && srcL.tabs.length <= 1;
  });
  // If we pre-populated the entry tab, the $effect below has nothing
  // to do; skip it so we don't double-trigger onSelectNode.
  let initialSelectionDone = !!initialTab || !!fsState?.initialLayout || !!fsState?.initialGroups;

  const focusedGroup = $derived(findLeaf(focusedGroupId) || findFirstLeaf(rootLayout));
  const activeTab = $derived(focusedGroup?.activeTab ?? null);
  const activeNode = $derived(
    activeTab && activeTab.type !== "wildcard" ? activeTab.node : null
  );
  const leafCount = $derived(allLeaves(rootLayout).length);

  function clickActivityIcon(view) {
    if (sidebarCollapsed) {
      sidebarCollapsed = false;
      activeView = view;
    } else if (activeView === view) {
      sidebarCollapsed = true;
    } else {
      activeView = view;
    }
    if (!sidebarCollapsed && view === "switch") overlayEl?._pcrRefreshSwitchPanel?.();
  }

  function setFocusedGroup(groupId) {
    focusedGroupId = groupId;
    // Always push to the bridge — `focusedGroupId` is seeded from saved
    // state while the bridge's focusedGroup becomes whichever EditorGroup
    // mounts first, so the two disagree at init. Skipping the push here
    // when groupId already equals focusedGroupId would leave that drift
    // forever; the bridge guards redundant calls internally.
    overlayEl?._pcrSetFocusedGroup?.(groupId);
  }

  // Tree click: if the node is already open in any pane, jump to that
  // pane's tab instead of opening a duplicate; otherwise add it to the
  // currently focused pane.
  function selectTreeNode(treeNode, scrollToWildcard) {
    if (!treeNode?.node) return;
    for (const g of allLeaves(rootLayout)) {
      const existing = g.tabs.find(t => t.node === treeNode.node);
      if (existing) {
        setFocusedGroup(g.id);
        g.activeTab = g.tabs.find(t => t.node === treeNode.node) || null;
        rootLayout = { ...rootLayout };
        onSelectNode(treeNode, scrollToWildcard);
        return;
      }
    }
    const leaf = focusedGroup;
    if (!leaf) return;
    onSelectNode(treeNode, scrollToWildcard);
    if (!leaf.tabs.find(t => t.node === treeNode.node)) {
      leaf.tabs = [...leaf.tabs, { node: treeNode.node, title: treeNode.title }];
    }
    // Re-find from leaf.tabs so activeTab refers to the proxied entry —
    // TabBar checks active class by identity and Svelte 5 state proxies
    // make the local literal !== the array slot.
    leaf.activeTab = leaf.tabs.find(t => t.node === treeNode.node) || null;
    rootLayout = { ...rootLayout };
  }

  function handleTabSelect(groupId, tab) {
    const leaf = findLeaf(groupId);
    if (!leaf) return;
    // Bail if the tab isn't in this pane anymore (e.g. the user just
    // dragged it to another pane — TabBar's ondragend fires after the
    // drop and would otherwise re-activate a tab that no longer exists
    // here, making the source pane resurrect the dragged tab).
    if (!leaf.tabs.find(t => sameTab(t, tab))) return;
    setFocusedGroup(groupId);
    leaf.activeTab = tab;
    rootLayout = { ...rootLayout };
    if (tab.type === "wildcard") {
      onWildcardClick(tab.wildcardName);
    } else if (tab.node) {
      onSelectNode({ node: tab.node, title: tab.title });
    }
  }

  // Match tabs by underlying identity (LGraphNode / wildcard filename)
  // rather than reference equality — Svelte 5 state proxies can make
  // `a === b` fail even for the same logical tab. Used across tab ops.
  function sameTab(a, b) {
    if (!a || !b) return false;
    if (a === b) return true;
    if (a.type === "wildcard") {
      return b.type === "wildcard" && a.filename === b.filename;
    }
    return !!a.node && a.node === b.node;
  }

  function handleTabClose(groupId, tab) {
    const leaf = findLeaf(groupId);
    if (!leaf) return;
    if (tab.type === "wildcard") overlayEl?._pcrFlushWildcard?.(tab.wildcardName);
    leaf.tabs = leaf.tabs.filter(t => !sameTab(t, tab));
    if (sameTab(tab, leaf.activeTab)) {
      if (leaf.tabs.length > 0) {
        handleTabSelect(groupId, leaf.tabs[leaf.tabs.length - 1]);
        return;
      }
      leaf.activeTab = null;
      if (groupId === focusedGroupId) overlayEl?._pcrClearWildcard?.();
    }
    rootLayout = { ...rootLayout };
    // Auto-collapse empty splits. With the tab bar hidden when a leaf
    // has no tabs, there's no UI to close the pane from — and leaving an
    // empty pane feels like a stuck divider.
    if (leaf.tabs.length === 0 && leafCount > 1) {
      closeGroup(groupId);
    }
  }

  // Invoked by TabBar for both same-pane reorder and cross-pane insert —
  // `groupId` is the target leaf. We locate the source leaf by scanning
  // all leaves for the dragged tab. Intra-pane just reorders in place;
  // cross-pane moves the tab, auto-closes an empty source pane, and
  // triggers the bridge to reload the source editor if the moved tab
  // was active there.
  function handleTabReorder(groupId, srcTab, targetTab, before) {
    const targetLeaf = findLeaf(groupId);
    if (!targetLeaf) return;
    const srcLeaf = allLeaves(rootLayout).find(l => l.tabs.some(t => sameTab(t, srcTab)));
    if (!srcLeaf) return;
    const srcTabData = srcLeaf.tabs.find(t => sameTab(t, srcTab));
    if (!srcTabData) return;

    if (srcLeaf.id === targetLeaf.id) {
      const filtered = targetLeaf.tabs.filter(t => !sameTab(t, srcTab));
      const targetIdx = filtered.findIndex(t => sameTab(t, targetTab));
      if (targetIdx < 0) return;
      filtered.splice(before ? targetIdx : targetIdx + 1, 0, srcTabData);
      targetLeaf.tabs = filtered;
      rootLayout = { ...rootLayout };
      return;
    }

    // Cross-pane insert.
    draggingTab = null;
    const srcWasActive = sameTab(srcLeaf.activeTab, srcTab);
    srcLeaf.tabs = srcLeaf.tabs.filter(t => !sameTab(t, srcTab));
    if (srcWasActive) {
      srcLeaf.activeTab = srcLeaf.tabs[srcLeaf.tabs.length - 1] || null;
    }
    const targetIdx = targetLeaf.tabs.findIndex(t => sameTab(t, targetTab));
    if (targetIdx < 0) return;
    const insertAt = before ? targetIdx : targetIdx + 1;
    targetLeaf.tabs = [
      ...targetLeaf.tabs.slice(0, insertAt),
      srcTabData,
      ...targetLeaf.tabs.slice(insertAt),
    ];
    targetLeaf.activeTab = srcTabData;
    setFocusedGroup(groupId);
    rootLayout = { ...rootLayout };
    if (srcWasActive) overlayEl?._pcrLoadTabInGroup?.(srcLeaf.id, srcLeaf.activeTab);
    if (srcTabData.type === "wildcard") onWildcardClick(srcTabData.wildcardName);
    else if (srcTabData.node) onSelectNode({ node: srcTabData.node, title: srcTabData.title });
    if (srcLeaf.tabs.length === 0 && leafCount > 1) closeGroup(srcLeaf.id);
  }

  // Insert a new leaf adjacent to targetLeafId in the given direction.
  // If the parent container already has that direction, splice the new
  // leaf in as a sibling. Otherwise wrap the target in a new container
  // of that direction (the new container takes over the target's flex;
  // both children inside get flex=1 so they share equally).
  function splitLeafInsert(targetLeafId, direction, before, newLeaf) {
    const parent = findParentContainer(targetLeafId);
    if (!parent) return false;
    const idx = parent.children.findIndex(c => c.kind === "leaf" && c.id === targetLeafId);
    if (idx < 0) return false;
    if (parent.direction === direction) {
      parent.children.splice(before ? idx : idx + 1, 0, newLeaf);
    } else {
      const targetLeaf = parent.children[idx];
      const prevFlex = targetLeaf.flex ?? 1;
      targetLeaf.flex = 1;
      const newContainer = {
        kind: "container",
        id: nextGroupId++,
        direction,
        flex: prevFlex,
        children: before ? [newLeaf, targetLeaf] : [targetLeaf, newLeaf],
      };
      parent.children.splice(idx, 1, newContainer);
    }
    rootLayout = { ...rootLayout };
    return true;
  }

  // After removing a leaf, the parent may be left with a single child.
  // VS Code collapses the degenerate container — replace it in the
  // grandparent with that sole child, inheriting the parent's flex so
  // the outer layout keeps its proportions. Never dissolves the root.
  function collapseUp(container) {
    if (!container || container === rootLayout) {
      if (container === rootLayout && container.children.length === 0) {
        container.children.push({
          kind: "leaf",
          id: nextGroupId++,
          tabs: [],
          activeTab: null,
          flex: 1,
        });
      }
      return;
    }
    if (container.children.length === 1) {
      const parent = findParentContainer(container.id);
      if (!parent) return;
      const pIdx = parent.children.findIndex(c => c.id === container.id);
      if (pIdx < 0) return;
      const sole = container.children[0];
      sole.flex = container.flex ?? 1;
      parent.children.splice(pIdx, 1, sole);
      collapseUp(parent);
    } else if (container.children.length === 0) {
      const parent = findParentContainer(container.id);
      if (!parent) return;
      parent.children = parent.children.filter(c => c.id !== container.id);
      collapseUp(parent);
    }
  }

  function closeGroup(groupId) {
    if (leafCount <= 1) return; // never close the last pane
    const leaf = findLeaf(groupId);
    if (!leaf) return;
    // Flush any wildcard save owned by this pane — bridge's destroy path
    // already no-ops if not owned, but calling here uses the same overlay
    // API surface that handleTabClose uses.
    if (leaf.activeTab?.type === "wildcard") {
      overlayEl?._pcrFlushWildcard?.(leaf.activeTab.wildcardName);
    }
    const parent = findParentContainer(groupId);
    if (!parent) return;
    parent.children = parent.children.filter(c => !(c.kind === "leaf" && c.id === groupId));
    collapseUp(parent);
    rootLayout = { ...rootLayout };
    if (focusedGroupId === groupId) {
      const first = findFirstLeaf(rootLayout);
      if (first) {
        focusedGroupId = first.id;
        queueMicrotask(() => overlayEl?._pcrSetFocusedGroup?.(focusedGroupId));
      }
    }
  }

  // Resizable splitter between adjacent panes inside a container. Each
  // node has a `flex` value used as flex-grow on its element; dragging
  // redistributes flex between the two adjacent children of `parent`,
  // using horizontal or vertical axis based on parent.direction.
  function startResize(parent, leftIdx, rightIdx, e) {
    e.preventDefault();
    e.stopPropagation();
    const handle = e.currentTarget;
    const parentEl = handle.parentElement;
    const rect = parentEl.getBoundingClientRect();
    const isRow = parent.direction === "row";
    const parentExtent = isRow ? rect.width : rect.height;
    const start = isRow ? e.clientX : e.clientY;
    const leftChild = parent.children[leftIdx];
    const rightChild = parent.children[rightIdx];
    if (!leftChild || !rightChild) return;
    const leftStartFlex = leftChild.flex ?? 1;
    const rightStartFlex = rightChild.flex ?? 1;
    const totalFlex = parent.children.reduce((s, c) => s + (c.flex ?? 1), 0);
    handle.setPointerCapture(e.pointerId);
    document.body.style.cursor = isRow ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
    const MIN = 0.1;
    const onMove = (ev) => {
      const current = isRow ? ev.clientX : ev.clientY;
      const deltaFlex = ((current - start) / parentExtent) * totalFlex;
      let newLeft = leftStartFlex + deltaFlex;
      let newRight = rightStartFlex - deltaFlex;
      if (newLeft < MIN) { newRight -= (MIN - newLeft); newLeft = MIN; }
      if (newRight < MIN) { newLeft -= (MIN - newRight); newRight = MIN; }
      leftChild.flex = newLeft;
      rightChild.flex = newRight;
      rootLayout = { ...rootLayout };
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  }

  // ── Tab drag-and-drop into pane drop zones ───────────────────────────
  function handleTabDragStart(groupId, tab) { draggingTab = { groupId, tab }; }
  function handleTabDragEnd() { draggingTab = null; }

  // A tab was dropped onto a pane's drop zone.
  //   center → move (or reorder) the tab into the target pane's tab list
  //   right/left → split target horizontally, new leaf on that side
  //   top/bottom → split target vertically, new leaf on that side
  function handleTabDrop(targetGroupId, zone) {
    const src = draggingTab;
    draggingTab = null;
    if (!src) return;
    const srcLeaf = findLeaf(src.groupId);
    const targetLeaf = findLeaf(targetGroupId);
    if (!srcLeaf || !targetLeaf) return;
    // VS Code rule: a single-tab pane can't be split into itself.
    if (srcLeaf === targetLeaf && srcLeaf.tabs.length <= 1) return;
    // Own-center with multi-tabs is a no-op — target already owns the tab.
    if (zone === "center" && srcLeaf === targetLeaf) return;

    const srcTab = srcLeaf.tabs.find(t => sameTab(t, src.tab));
    if (!srcTab) return;
    const srcWasActive = sameTab(srcLeaf.activeTab, src.tab);

    if (zone === "center") {
      srcLeaf.tabs = srcLeaf.tabs.filter(t => !sameTab(t, src.tab));
      if (srcWasActive) {
        srcLeaf.activeTab = srcLeaf.tabs[srcLeaf.tabs.length - 1] || null;
      }
      let moved = targetLeaf.tabs.find(t => sameTab(t, src.tab));
      if (!moved) {
        targetLeaf.tabs = [...targetLeaf.tabs, srcTab];
        moved = targetLeaf.tabs[targetLeaf.tabs.length - 1];
      }
      targetLeaf.activeTab = moved;
      setFocusedGroup(targetGroupId);
      rootLayout = { ...rootLayout };
      if (srcWasActive) overlayEl?._pcrLoadTabInGroup?.(srcLeaf.id, srcLeaf.activeTab);
      if (srcTab.type === "wildcard") onWildcardClick(srcTab.wildcardName);
      else if (srcTab.node) onSelectNode({ node: srcTab.node, title: srcTab.title });
      if (srcLeaf.tabs.length === 0 && leafCount > 1) closeGroup(srcLeaf.id);
      return;
    }

    // Edge zones — split target in the appropriate direction.
    const direction = (zone === "left" || zone === "right") ? "row" : "column";
    const before = (zone === "left" || zone === "top");

    srcLeaf.tabs = srcLeaf.tabs.filter(t => !sameTab(t, src.tab));
    if (srcWasActive) {
      srcLeaf.activeTab = srcLeaf.tabs[srcLeaf.tabs.length - 1] || null;
    }
    const newLeaf = {
      kind: "leaf",
      id: nextGroupId++,
      tabs: [srcTab],
      activeTab: srcTab,
      flex: 1,
    };
    if (!splitLeafInsert(targetGroupId, direction, before, newLeaf)) return;
    focusedGroupId = newLeaf.id;
    if (srcWasActive) overlayEl?._pcrLoadTabInGroup?.(srcLeaf.id, srcLeaf.activeTab);
    queueMicrotask(() => overlayEl?._pcrSetFocusedGroup?.(newLeaf.id));
    queueMicrotask(() => {
      if (srcTab.type === "wildcard") onWildcardClick(srcTab.wildcardName);
      else if (srcTab.node) onSelectNode({ node: srcTab.node, title: srcTab.title });
    });
    // Source became empty from pulling its only tab out — auto-close.
    if (srcLeaf.tabs.length === 0 && leafCount > 1) closeGroup(srcLeaf.id);
  }

  // Fallback auto-select for the rare case where findEntryTab() returned
  // null at script eval (treeRoots not yet populated). The $effect re-runs
  // when treeRoots becomes non-empty.
  $effect(() => {
    if (initialSelectionDone) return;
    if (fsState.treeRoots.length > 0) {
      initialSelectionDone = true;
      let target = null;
      if (entryNodeId) {
        // Cycle guard: a corrupted graph or a race during node removal
        // can produce a tree with back-edges.  Without the visited set,
        // recursion would blow the stack and kill the extension.
        function findInTree(tree, visited) {
          if (visited.has(tree)) return null;
          visited.add(tree);
          if (tree.node.id === entryNodeId) return tree;
          for (const child of tree.children) {
            const found = findInTree(child, visited);
            if (found) return found;
          }
          return null;
        }
        for (const root of fsState.treeRoots) {
          target = findInTree(root, new Set());
          if (target) break;
        }
      }
      if (!target) target = fsState.treeRoots[0];
      if (target?.node) selectTreeNode(target);
    }
  });

  function stopProp(e) { e.stopPropagation(); }

  // overlay API: expose state updaters for the bridge to call imperatively.
  // All operations target the focused group unless the payload identifies
  // a specific tab (e.g. close-wildcard-tab scans every group).
  onMount(() => {
    if (!overlayEl) return;
    overlayEl._pcrAddWildcardTab = (wildcardName, filename) => {
      const leaf = focusedGroup;
      if (!leaf) return;
      if (!leaf.tabs.find(t => t.type === "wildcard" && t.filename === filename)) {
        leaf.tabs = [...leaf.tabs, { node: null, title: filename, type: "wildcard", wildcardName, filename }];
      }
      // Re-find from the array so the reference matches TabBar's strict-eq.
      leaf.activeTab = leaf.tabs.find(t => t.type === "wildcard" && t.filename === filename) || null;
      rootLayout = { ...rootLayout };
    };
    overlayEl._pcrHideWelcome = () => { /* welcome is derived from leaf.activeTab */ };
    overlayEl._pcrShowWelcome = () => {
      const leaf = focusedGroup;
      if (leaf) { leaf.activeTab = null; rootLayout = { ...rootLayout }; }
    };
    overlayEl._pcrUpdateWorkflowName = (name) => { liveWorkflowName = name || "Workflow"; };
    overlayEl._pcrUpdateTabTitle = (nodeId, newTitle) => {
      let changed = false;
      for (const g of allLeaves(rootLayout)) {
        const tab = g.tabs.find(t => t.node?.id === nodeId);
        if (tab) { tab.title = newTitle; changed = true; }
      }
      if (changed) rootLayout = { ...rootLayout };
    };
    overlayEl._pcrCloseWildcardTab = (filename) => {
      for (const g of allLeaves(rootLayout)) {
        const tab = g.tabs.find(t => t.type === "wildcard" && t.filename === filename);
        if (tab) { handleTabClose(g.id, tab); return; }
      }
    };
    overlayEl._pcrGetActiveWildcardTab = () => {
      return focusedGroup?.activeTab?.type === "wildcard" ? focusedGroup.activeTab : null;
    };
    // Bridge notifies us when CM6 gained focus so we keep focusedGroupId
    // in sync for tree clicks / split buttons without requiring the user
    // to click in the column's padding.
    overlayEl._pcrNotifyFocusChange = (groupId) => {
      if (focusedGroupId !== groupId) focusedGroupId = groupId;
    };
    // Sidebar collapse state for the bridge to persist on exit.
    overlayEl._pcrGetSidebarState = () => ({ collapsed: sidebarCollapsed, view: activeView });
    // Serialize pane layout for persistence. Bridge writes this to
    // root.properties.pcrFsGroups; on reload the bridge hydrates nodeIds
    // back into LGraphNode refs and hands us the resolved tree.
    overlayEl._pcrGetFsGroups = () => ({
      root: serializeLayoutNode(rootLayout),
      focusedLeafId: focusedGroupId,
    });
  });

  function serializeLayoutNode(node) {
    if (!node) return null;
    if (node.kind === "container") {
      return {
        kind: "container",
        direction: node.direction,
        flex: node.flex,
        children: node.children.map(serializeLayoutNode).filter(Boolean),
      };
    }
    return {
      kind: "leaf",
      flex: node.flex,
      tabs: node.tabs.map(t => t.type === "wildcard"
        ? { kind: "wildcard", wildcardName: t.wildcardName, filename: t.filename, title: t.title }
        : { kind: "node", nodeId: t.node?.id, title: t.title }
      ),
      activeTabIdx: node.activeTab ? node.tabs.findIndex(t => sameTab(t, node.activeTab)) : -1,
    };
  }

  // keyboard shortcuts: keydown + document-level Ctrl+S capture
  onMount(() => {
    if (!overlayEl) return;
    function handleKeydown(e) {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        e.stopImmediatePropagation();
        onSaveWorkflow();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        // Focus inside a CM editor: defer to its bundle keymap. The previous
        // loose check matched Ctrl+Alt+Enter and Ctrl+Shift+Enter too,
        // queueing a prompt when the user meant to interrupt or queue-front.
        if (e.target.closest(".cm-editor")) return;
        e.preventDefault();
        if (e.altKey) {
          window.app?.extensionManager?.command?.execute?.("Comfy.Interrupt");
        } else if (e.shiftKey) {
          window.app?.extensionManager?.command?.execute?.("Comfy.QueuePromptFront");
        } else {
          onQueuePrompt(1);
        }
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "`") {
        e.preventDefault();
        overlayEl.querySelector('[title="Toggle output panel"]')?.click();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "w") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (activeTab) handleTabClose(focusedGroupId, activeTab);
      }
      if (e.key === "F2" && activeNode) {
        e.preventDefault();
        fsState.renamingNodeId = activeNode.id;
      }
    }
    overlayEl.addEventListener("keydown", handleKeydown);

    function captureSave(e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
      }
    }
    document.addEventListener("keydown", captureSave, true);

    return () => {
      overlayEl?.removeEventListener("keydown", handleKeydown);
      document.removeEventListener("keydown", captureSave, true);
    };
  });

  // pointer/wheel event isolation: prevent events from leaking to canvas
  onMount(() => {
    if (!overlayEl) return;
    const stopPropEvents = ["keyup", "keypress", "mousedown", "mouseup",
      "click", "dblclick", "pointerup", "copy", "paste", "cut"];
    for (const evt of stopPropEvents) {
      overlayEl.addEventListener(evt, stopProp);
    }

    function handlePointerDown(e) {
      e.stopPropagation();
      // Indicator's own click handler owns the popup toggle. Skip auto-close
      // here — Svelte 5 delegates the indicator's pointerdown to the document
      // root, which runs *after* this bubble-phase listener, so its
      // stopPropagation can't prevent us from firing. Closing here would
      // defeat the isPopupOpen() guard and cause close-then-reopen flicker.
      if (e.target.closest(".pcr-nettree-indicator")) return;
      overlayEl._pcrClosePopup?.();
    }
    overlayEl.addEventListener("pointerdown", handlePointerDown);

    function handleMouseDown(e) {
      const editorEl = overlayEl.querySelector(".pcr-fs-editor-body .cm-editor");
      if (editorEl && !editorEl.contains(e.target)
          && e.target.tagName !== "INPUT"
          && e.target.tagName !== "TEXTAREA"
          && !e.target.closest("[draggable]")
          && !e.target.closest(".pcr-nettree-indicator")
          && !e.target.closest(".pcr-output-panel-content")
          && !e.target.closest(".pcr-ai-panel")
          // The docked 3D Poser manages its own pointer events and hosts native
          // <select>s (hand presets) — preventDefault here would block them from
          // opening. Let it behave natively, same as in node view.
          && !e.target.closest(".pcr-pose-panel")) {
        e.preventDefault();
      }
    }
    overlayEl.addEventListener("mousedown", handleMouseDown);

    function handleWheel(e) {
      e.stopPropagation();

      const imagePanel = e.target.closest(".pcr-image-panel");
      if (imagePanel) {
        e.preventDefault();
        const rect = imagePanel.getBoundingClientRect();
        imagePanel.dispatchEvent(new CustomEvent("pcr-zoom", {
          detail: {
            deltaY: e.deltaY,
            mouseX: e.clientX - rect.left,
            mouseY: e.clientY - rect.top,
            containerWidth: rect.width,
            containerHeight: rect.height,
          },
        }));
        return;
      }

      if (e.ctrlKey) {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -1 : 1;
        if (e.target.closest(".pcr-fs-sidebar")) return;

        const outputPanel = e.target.closest(".pcr-output-panel");
        if (outputPanel) {
          if (e.target.closest(".pcr-output-panel-generated")) {
            outputPanel._updateGalleryZoom?.(delta);
          } else if (e.target.closest(".pcr-output-panel-content, .pcr-console-log")) {
            const area = overlayEl.querySelector(".pcr-fs-editor-area");
            if (area) {
              const cur = parseFloat(getComputedStyle(area).getPropertyValue("--pcr-output-font-size")) || 13;
              const next = Math.max(8, Math.min(32, cur + delta));
              area.style.setProperty("--pcr-output-font-size", `${next}px`);
              overlayEl._pcrSetOutputFontSize?.(next);
            }
          }
          return;
        }

        const editorBody = e.target.closest(".pcr-fs-editor-body");
        if (editorBody) overlayEl?._pcrUpdateFsFontSize?.(delta);
      }
    }
    overlayEl.addEventListener("wheel", handleWheel, { passive: false });

    return () => {
      for (const evt of stopPropEvents) overlayEl?.removeEventListener(evt, stopProp);
      overlayEl?.removeEventListener("pointerdown", handlePointerDown);
      overlayEl?.removeEventListener("mousedown", handleMouseDown);
      overlayEl?.removeEventListener("wheel", handleWheel);
    };
  });
</script>

<Topbar
  {logoUrl}
  workflowName={liveWorkflowName}
  {onQueuePrompt}
  {onCancelExecution}
  onClose={() => onClose()}
/>

<div class="pcr-fs-body">
  <div class="pcr-fs-sidebar" style:width={sidebarCollapsed ? "auto" : `${sidebarWidth}px`}>
    <div class="pcr-activity-bar">
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-activity-icon pcr-activity-icon--comfy"
        bind:this={comfyIconEl}
        class:pcr-activity-icon--active={comfyMenuOpen}
        title="Menu"
        onclick={(e) => { e.stopPropagation(); toggleComfyMenu(); }}>
        <svg width="20" height="20" viewBox="0 0 18 18" fill="currentColor"><path d="M14.8193 0.600586C15.1248 0.600586 15.3296 0.70893 15.459 0.881836C15.5914 1.05888 15.6471 1.33774 15.5527 1.66895L14.8037 4.30176C14.7063 4.64386 14.4729 4.97024 14.1641 5.21191C13.8544 5.45415 13.496 5.58984 13.1699 5.58984H13.1689L9.5791 5.59668H7.90625C7.52654 5.59668 7.19496 5.84986 7.09082 6.21289L5.69434 11.0889C5.63007 11.3133 5.66134 11.5534 5.77734 11.7529L5.83203 11.8359C5.99177 12.0491 6.24252 12.1758 6.50977 12.1758H6.51074L8.88281 12.1709H11.4971C11.7643 12.171 11.9541 12.254 12.084 12.3906L12.1357 12.4521C12.2685 12.6295 12.3249 12.9089 12.2305 13.2402L11.4805 15.8721C11.383 16.2144 11.1498 16.5415 10.8408 16.7832C10.5314 17.0252 10.1736 17.161 9.84766 17.1611H9.84668L6.25684 17.168H3.64258C3.33762 17.1679 3.13349 17.0588 3.00391 16.8857C2.87135 16.7087 2.81482 16.43 2.90918 16.0986L3.39551 14.3887C3.46841 14.1327 3.41794 13.8576 3.25879 13.6445V13.6436C3.09901 13.4303 2.84745 13.3037 2.58008 13.3037H1.18066C0.875088 13.3037 0.670398 13.1953 0.541016 13.0225C0.408483 12.8451 0.351891 12.5655 0.446289 12.2344L2.11914 6.38965L2.30371 5.74707V5.74609C2.40139 5.40341 2.63456 5.07671 2.94336 4.83496C3.25302 4.59258 3.61143 4.45705 3.9375 4.45703H5.6123C5.94484 4.45703 6.24083 4.26316 6.37891 3.9707L6.42773 3.83984L6.98145 1.89551C7.07894 1.55317 7.31212 1.22614 7.62109 0.984375C7.93074 0.742127 8.2892 0.606445 8.61523 0.606445H8.61621L12.1982 0.600586H14.8193Z"/></svg>
      </span>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-activity-icon"
        class:pcr-activity-icon--active={!sidebarCollapsed && activeView === "edit"}
        data-view="edit" title="Edit"
        onclick={() => clickActivityIcon("edit")}>
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM17.5 14v7M14 17.5h7"/></svg>
      </span>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-activity-icon"
        class:pcr-activity-icon--active={!sidebarCollapsed && activeView === "switch"}
        data-view="switch" title="Switch"
        onclick={() => clickActivityIcon("switch")}>
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6h13M6 12h13M6 18h13M1 6h.01M1 12h.01M1 18h.01"/></svg>
      </span>

      {#if comfyMenuOpen}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pcr-comfy-menu-backdrop" onclick={() => comfyMenuOpen = false}></div>
        <div class="pcr-comfy-menu" style:left={`${comfyMenuPos.left}px`} style:top={`${comfyMenuPos.top}px`}>
          {#each COMFY_MENU_ITEMS as item}
            {#if item.sep}
              <div class="pcr-comfy-menu-sep"></div>
            {:else}
              <button class="pcr-comfy-menu-item" onclick={() => runComfyCommand(item.command)}>{item.label}</button>
            {/if}
          {/each}
        </div>
      {/if}
    </div>

    <div class="pcr-fs-sidebar-content" style:display={sidebarCollapsed ? "none" : ""}>
      <div class="pcr-fs-sidebar-panel" data-panel="edit" style:display={activeView === "edit" ? "" : "none"}>
        <div class="pcr-nettree-header">
          <span class="pcr-nettree-header-label">Network</span>
          <div class="pcr-nettree-header-actions">
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <span class="pcr-nettree-header-btn" title="Add node" onclick={() => onAddNode()}>+</span>
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <span class="pcr-nettree-header-btn" title="Refresh tree" onclick={() => refreshTree()}>{"\u21BB"}</span>
          </div>
        </div>
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="pcr-nettree-items"
          oncontextmenu={(e) => { e.preventDefault(); e.stopPropagation(); onEmptyContextMenu(e.clientX, e.clientY); }}>
          <NetworkTree
            roots={fsState.treeRoots}
            activeNodeId={activeNode?.id}
            renamingNodeId={fsState.renamingNodeId}
            onSelectNode={selectTreeNode}
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
          />
        </div>
      </div>
      <div class="pcr-fs-sidebar-panel" data-panel="switch" style:display={activeView === "switch" ? "" : "none"}>
        <div class="pcr-nettree-header">
          <span class="pcr-nettree-header-label">Switches</span>
        </div>
        <div class="pcr-switch-items"></div>
      </div>
    </div>
  </div>

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  {#if !sidebarCollapsed}
  <div class="pcr-fs-sidebar-resize" onpointerdown={(e) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startWidth = sidebarWidth;
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const onMove = (ev) => { sidebarWidth = Math.max(150, Math.min(500, startWidth + ev.clientX - startX)); };
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      e.currentTarget.removeEventListener("pointermove", onMove);
      e.currentTarget.removeEventListener("pointerup", onUp);
    };
    e.currentTarget.addEventListener("pointermove", onMove);
    e.currentTarget.addEventListener("pointerup", onUp);
  }}></div>
  {/if}

  <div class="pcr-fs-main">
    <!-- Left column: stacked panes on top, full-width output panel below.
         The output panel (and its resize handle) is relocated here by the
         bridge into .pcr-fs-editor-area so its parentElement has a known
         flex-filled height for the resize-drag math to work correctly. -->
    <div class="pcr-fs-editor-area">
    <div class="pcr-fs-content-row">
      <LayoutNode
        node={rootLayout}
        {overlayEl}
        focusedLeafId={focusedGroupId}
        {draggingTab}
        {dragSourceSingleTab}
        treeRoots={fsState.treeRoots}
        {logoTextUrl}
        {leafCount}
        onFocus={setFocusedGroup}
        onSelectTab={handleTabSelect}
        onCloseTab={handleTabClose}
        onReorderTabs={handleTabReorder}
        onTabDragStart={handleTabDragStart}
        onTabDragEnd={handleTabDragEnd}
        onTabDrop={handleTabDrop}
        onCloseGroup={closeGroup}
        onStartResize={startResize}
      />
    </div>
    </div>
    <!-- Image panel and its divider are relocated here by the bridge — as
         siblings of .pcr-fs-editor-area so they span the full height and
         don't get clipped by the output panel row below the editor area. -->
  </div>
</div>

<!-- footer slot — bridge relocates active node's footer here -->
<div class="pcr-fs-footer-slot"></div>

<style>
  /* ── Root overlay ── */
  /* Host PrimeVue dialogs (Settings, Save As) get an autoZIndex around 1100,
     below this overlay's 9999, so they open behind it. While the overlay is
     present, lift their mask above it. !important is required to beat the
     inline z-index PrimeVue writes on the mask element. */
  :global(:root:has(.pcr-fs-overlay) .p-dialog-mask) {
    z-index: 10050 !important;
  }
  :global(.pcr-fs-overlay) {
    position: fixed;
    inset: 0;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    background: #1e1e1e;
    font-family: system-ui, -apple-system, sans-serif;
    font-size: 13px;
    color: #ccc;
    /* Unified editor surface: code editor, active tab, breadcrumb. */
    --pcr-fs-editor-surface: #161616;
    /* Secondary chrome surface: sidebar, topbar, tabbar, output/footer header. */
    --pcr-fs-chrome-surface: #1c1c1c;
    /* Muted foreground: inactive activity icons, section header labels. */
    --pcr-fs-muted-text: #ffffff82;
  }

  /* ── Body layout ── */
  .pcr-fs-body {
    flex: 1;
    display: flex;
    min-height: 0;
  }

  /* ── Sidebar ── */
  .pcr-fs-sidebar {
    width: 260px;
    flex-shrink: 0;
    background: var(--pcr-fs-chrome-surface);
    display: flex;
    flex-direction: row;
    overflow: hidden;
  }
  .pcr-fs-sidebar-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
  }
  .pcr-fs-sidebar-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .pcr-fs-sidebar-resize {
    width: 1px;
    flex-shrink: 0;
    cursor: col-resize;
    background: #3c3c3c;
    transition: background 0.15s;
  }
  .pcr-fs-sidebar-resize:hover { background: rgba(79, 195, 247, 0.4); }
  .pcr-fs-sidebar-resize:active { background: rgba(79, 195, 247, 0.8); }

  /* ── Activity bar ── */
  .pcr-activity-bar {
    position: relative;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    padding-top: 0;
    gap: 2px;
    border-right: 1px solid #2a2a2a;
  }
  .pcr-activity-icon {
    width: 50px;
    height: 50px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: var(--pcr-fs-muted-text);
    border-radius: 0;
    transition: color 0.15s, background 0.15s;
    border-left: 2px solid transparent;
  }
  .pcr-activity-icon:hover { color: #ff8729e6; background: rgba(255,255,255,0.05); }
  .pcr-activity-icon--active {
    color: #ff8729e6;
    border-left-color: #ff811fcc;
  }

  /* Comfy menu dropdown (mirrors the host sidebar logo menu) */
  .pcr-comfy-menu-backdrop {
    position: fixed;
    inset: 0;
    z-index: 10000;
  }
  .pcr-comfy-menu {
    position: fixed;
    z-index: 10001;
    min-width: 170px;
    background: rgba(38, 38, 38, 0.92);
    backdrop-filter: blur(20px) saturate(180%);
    border: 1px solid rgba(52, 52, 52, 0.6);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    padding: 4px 0;
  }
  .pcr-comfy-menu-item {
    display: block;
    width: 100%;
    padding: 8px 14px;
    border: none;
    background: transparent;
    color: var(--input-text, #fff);
    font-size: 13px;
    text-align: left;
    cursor: pointer;
    transition: background-color 0.15s;
  }
  .pcr-comfy-menu-item:hover { background: rgba(255, 255, 255, 0.1); }
  .pcr-comfy-menu-sep {
    height: 1px;
    margin: 4px 0;
    background: rgba(255, 255, 255, 0.08);
  }

  /* ── Network tree header (rendered by FullscreenEditor) ── */
  .pcr-nettree-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 12px;
    height: 40px;
    border-bottom: 1px solid #2a2a2a;
    flex-shrink: 0;
  }
  .pcr-nettree-header-label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    color: var(--pcr-fs-muted-text);
    text-transform: uppercase;
  }
  .pcr-nettree-header-actions {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .pcr-nettree-header-btn {
    background: none;
    border: none;
    color: #888;
    font-size: 14px;
    cursor: pointer;
    padding: 2px 6px;
    border-radius: 4px;
    transition: background 0.15s, color 0.15s;
    line-height: 1;
  }
  .pcr-nettree-header-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #ccc;
  }
  .pcr-nettree-items {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
  }
  .pcr-nettree-items::-webkit-scrollbar { width: 6px; }
  .pcr-nettree-items::-webkit-scrollbar-track { background: transparent; }
  .pcr-nettree-items::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 3px; }
  .pcr-nettree-items::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.25); }

  /* ── Switch panel ── */
  .pcr-switch-items {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
  }
  .pcr-switch-items::-webkit-scrollbar { width: 6px; }
  .pcr-switch-items::-webkit-scrollbar-track { background: transparent; }
  .pcr-switch-items::-webkit-scrollbar-thumb { background: #444; border-radius: 3px; }
  :global(.pcr-switch-row) {
    display: flex;
    align-items: center;
    padding: 5px 12px;
    cursor: pointer;
    transition: background 0.1s;
    gap: 8px;
  }
  :global(.pcr-switch-row:hover) { background: rgba(255, 255, 255, 0.06); }
  :global(.pcr-switch-label) {
    font-size: 12px;
    color: #bbb;
    white-space: nowrap;
    flex-shrink: 0;
  }
  :global(.pcr-switch-label--wildcard) {
    color: #c9a84c;
    font-style: italic;
  }
  :global(.pcr-switch-selector) {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
    padding: 2px 6px;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.15s;
  }
  :global(.pcr-switch-selector:hover) { background: rgba(255, 255, 255, 0.08); }
  :global(.pcr-switch-emoji) { font-size: 12px; }
  :global(.pcr-switch-value) {
    font-size: 11px;
    white-space: nowrap;
    max-width: 100px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  :global(.pcr-switch-arrow) {
    font-size: 10px;
    color: rgba(255, 255, 255, 0.4);
  }
  :global(.pcr-switch-empty) {
    padding: 20px 12px;
    color: #555;
    font-size: 12px;
    text-align: center;
  }

  /* ── Main editor area ──
     Main is now a row: editor-area (panes + output panel stacked) on
     the left, image panel on the right spanning full height. */
  .pcr-fs-main {
    flex: 1;
    display: flex;
    flex-direction: row;
    background: #1e1e1e;
    min-width: 0;
    overflow: hidden;
  }
  .pcr-fs-editor-area {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-content-row {
    flex: 1;
    display: flex;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }
  /* Column + editor-body rules moved to EditorGroup.svelte. */

  /* ── Fullscreen output panel overrides ── */
  :global(.pcr-fs-output-resize) {
    height: 4px;
    cursor: ns-resize;
    background: #3c3c3c;
    flex-shrink: 0;
  }
  :global(.pcr-fs-output-resize:hover) { background: rgba(79, 195, 247, 0.4); }
  :global(.pcr-fs-output-resize:active) { background: rgba(79, 195, 247, 0.8); }
  :global(.pcr-fs-output-panel) {
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    background: #1a1a1a;
    overflow: hidden;
  }
  :global(.pcr-fs-output-header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    background: var(--pcr-fs-chrome-surface);
    border-bottom: 1px solid #2a2a2a;
    flex-shrink: 0;
  }
  :global(.pcr-fs-output-title) {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #888;
  }
  :global(.pcr-fs-output-close) {
    cursor: pointer;
    color: #666;
    font-size: 12px;
    padding: 2px 4px;
  }
  :global(.pcr-fs-output-close:hover) { color: #fff; }
  :global(.pcr-fs-output-panel .pcr-output-panel-content) {
    flex: 1;
    overflow: auto;
    padding: 6px 10px;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ── Cross-component overrides (relocated panels) ── */
  .pcr-fs-main > :global(.pcr-image-panel) {
    flex-shrink: 1;
  }
  .pcr-fs-main > :global(.pcr-image-divider) {
    flex-shrink: 0;
  }
  /* 3D Poser panel is prepended (left of editor-area) by the bridge. */
  .pcr-fs-main > :global(.pcr-pose-panel) {
    flex-shrink: 1;
  }
  .pcr-fs-main > :global(.pcr-pose-divider) {
    flex-shrink: 0;
  }
  /* AI panel placement is per-pane — see .pcr-fs-editor-pane-row in
     EditorGroup.svelte. The bridge relocates the focused node's panel
     into that wrapper so it docks alongside CodeMirror, under the tab. */
  /* .pcr-fs-editor-column output-panel rules moved to EditorGroup.svelte. */

  /* Output panel is relocated into .pcr-fs-editor-area by the bridge.
     Styles preserve flex-shrink and header background so the panel keeps
     its node-mode appearance. */
  .pcr-fs-editor-area > :global(.pcr-output-panel) {
    flex-shrink: 0;
  }
  .pcr-fs-editor-area > :global(.pcr-output-panel-resize) {
    flex-shrink: 0;
  }
  .pcr-fs-editor-area :global(.pcr-output-panel-header) {
    background: var(--pcr-fs-chrome-surface);
  }

  /* Pane splitter styles moved to LayoutNode.svelte so they reach the
     recursive splitters at every nesting depth. */

  /* ── Footer slot ── */
  .pcr-fs-footer-slot {
    flex-shrink: 0;
    height: 30px;
    background: var(--pcr-fs-chrome-surface);
    border-top: 1px solid #3c3c3c;
  }
  .pcr-fs-footer-slot:has(:global(.pcr-footer)) {
    border-top: none;
  }
  .pcr-fs-footer-slot > :global(.pcr-footer) {
    background: var(--pcr-fs-chrome-surface);
  }

  /* ── Legacy tree/card styles (used by vanilla JS) ── */
  :global(.pcr-fs-tree) {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
  }
  :global(.pcr-fs-tree::-webkit-scrollbar) { width: 8px; }
  :global(.pcr-fs-tree::-webkit-scrollbar-track) { background: transparent; }
  :global(.pcr-fs-tree::-webkit-scrollbar-thumb) { background: #555; border-radius: 4px; }
  :global(.pcr-fs-card) {
    padding: 6px 12px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    transition: background 0.1s;
    user-select: none;
    min-height: 28px;
    border-left: 2px solid transparent;
  }
  :global(.pcr-fs-card:hover) { background: #2a2d2e; }
  :global(.pcr-fs-card-dragging) { opacity: 0.4; }
  :global(.pcr-fs-card-drop-before) { box-shadow: inset 0 2px 0 0 #4fc3f7; }
  :global(.pcr-fs-card-drop-after) { box-shadow: inset 0 -2px 0 0 #4fc3f7; }
  :global(.pcr-fs-card-active) {
    background: #37373d;
    border-left-color: #4fc3f7;
  }
  :global(.pcr-fs-card-indent) {
    font-size: 10px;
    width: 14px;
    text-align: center;
    flex-shrink: 0;
    color: #666;
  }
  :global(.pcr-fs-card-title) {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #ddd;
    font-size: 13px;
  }
  :global(.pcr-fs-card-active .pcr-fs-card-title) { color: #fff; }
  :global(.pcr-fs-card-badge) {
    flex-shrink: 0;
    font-size: 12px;
    padding: 1px 4px;
    border-radius: 3px;
    cursor: pointer;
  }
  :global(.pcr-fs-card-badge:hover) { background: rgba(255, 255, 255, 0.1); }
  :global(.pcr-fs-card-indicator) {
    flex-shrink: 0;
    font-size: 11px;
    opacity: 0.6;
  }
</style>
