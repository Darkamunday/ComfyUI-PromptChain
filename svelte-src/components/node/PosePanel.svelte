<script>
  // 3D Poser panel — left-side surface that hosts the live PromptChain_PoseStudio
  // viewport. Opening it reparents ("docks") the active Poser node's viewport DOM
  // into this panel so the figure is posed from one spot — and, because it's part
  // of the PromptChain node's own widget tree, it rides along into fullscreen.
  //
  // The viewport is a single relocatable element with one WebGL context; we move
  // it in/out rather than duplicating it. getActivePoser() comes from main.js,
  // which shares the pose registry with pose-studio.js (a direct import here would
  // bundle a second, non-shared copy).

  import { onMount, onDestroy } from "svelte";
  import { useApi } from "../../lib/api-context.js";

  const DEFAULT_WIDTH = 320;
  const MIN_WIDTH = 200;

  let {
    node,
    shared,
    onToggle = () => {},
    onRegister = null,
    getActivePoser = () => null,
  } = $props();

  const { getCanvasScale } = useApi();

  let panelEl;
  let dividerEl;
  let bodyEl;
  let isVisible = $state(!!node.properties?.pcrPosePanel);
  let panelWidth = $state(node.properties?.pcrPosePanelWidth || DEFAULT_WIDTH);
  // The Poser node whose viewport is currently docked here (so hide()/refresh
  // can return it home). Plain let — its identity drives imperative dock calls,
  // not template reactivity; `hasDock` mirrors it for the placeholder.
  let dockedNode = null;
  let hasDock = $state(false);

  // External visibility subscriber (the fullscreen bridge's topbar-button sync).
  // Fires on every visibility change, including this panel's own close button —
  // which calls hide() in component scope, bypassing any API wrapper.
  let toggleListener = null;
  function emitToggle(visible) {
    onToggle(visible);
    toggleListener?.(visible);
  }
  export function setToggleListener(cb) { toggleListener = cb; }

  function dock(poser) {
    if (!poser || !bodyEl || !poser._pcrPose?.enterDock) return;
    poser._pcrPose.enterDock(bodyEl);
    dockedNode = poser;
    hasDock = true;
  }

  function undock() {
    if (dockedNode && dockedNode._pcrPose?.exitDock) {
      try { dockedNode._pcrPose.exitDock(); } catch (e) { console.error("[PromptChain] pose undock error", e); }
    }
    dockedNode = null;
    hasDock = false;
  }

  // Re-evaluate which Poser is hosted. Called by main.js when the registry
  // changes (a Poser was added/removed). Keep the current host while it's alive;
  // only re-dock when it's gone, so switching focus doesn't yank the panel.
  export function refreshDock() {
    if (!isVisible) return;
    if (dockedNode && dockedNode._pcrAlive) return;
    if (dockedNode) undock(); // host died — release before re-docking
    const active = getActivePoser();
    if (active) dock(active);
  }

  export function show() {
    isVisible = true;
    if (node.properties) node.properties.pcrPosePanel = true;
    emitToggle(true);
    // bodyEl is bound by now (panel is always in the tree); dock on next frame
    // in case this fires before layout gives the body a height.
    requestAnimationFrame(() => { if (isVisible) dock(getActivePoser()); });
  }

  export function hide() {
    undock();
    isVisible = false;
    if (node.properties) node.properties.pcrPosePanel = false;
    emitToggle(false);
  }

  export function toggle() { isVisible ? hide() : show(); }
  export function getIsVisible() { return isVisible; }

  function getMaxPanelWidth() {
    const row = panelEl?.parentElement;
    const rowWidth = row?.offsetWidth || 0;
    if (!rowWidth) return Number.POSITIVE_INFINITY;
    // leave at least a sliver for the rest of the row
    return Math.max(MIN_WIDTH, rowWidth - 120);
  }

  function clampPanelWidth(value) {
    return Math.min(Math.max(MIN_WIDTH, value), getMaxPanelWidth());
  }

  onMount(() => {
    onRegister?.({ toggle, show, hide, getIsVisible, refreshDock, setToggleListener, cleanup });
    if (node.properties?.pcrPosePanel) requestAnimationFrame(() => show());
    requestAnimationFrame(() => { panelWidth = clampPanelWidth(panelWidth); });
  });

  // divider resize — panel is leftmost, so dragging the divider right widens it.
  let dividerAc;
  onMount(() => {
    dividerAc = new AbortController();
    let isDragging = false;
    let startX = 0;
    let startWidth = 0;
    const getScale = () => {
      const inFs = !!document.querySelector(".pcr-fs-overlay");
      return inFs ? 1 : getCanvasScale();
    };

    document.addEventListener("pointerdown", (e) => {
      if (!dividerEl || (e.target !== dividerEl && !dividerEl.contains(e.target))) return;
      isDragging = true;
      startX = e.clientX;
      startWidth = panelEl?.offsetWidth || panelWidth;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      const canvas = document.querySelector("canvas.lgraphcanvas");
      if (canvas && typeof e.pointerId === "number") {
        try { if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId); } catch {}
      }
    }, { capture: true, signal: dividerAc.signal });

    document.addEventListener("pointermove", (e) => {
      if (!isDragging) return;
      e.preventDefault();
      e.stopPropagation();
      const delta = (e.clientX - startX) / getScale();
      panelWidth = clampPanelWidth(startWidth + delta);
    }, { capture: true, signal: dividerAc.signal });

    document.addEventListener("pointerup", () => {
      if (!isDragging) return;
      isDragging = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (node.properties) node.properties.pcrPosePanelWidth = panelWidth;
    }, { capture: true, signal: dividerAc.signal });
  });

  // Close-✕ visibility: ghosted at 30% while the cursor is anywhere in the
  // panel, full strength in the TOP-RIGHT corner zone (top 10% x right 10%,
  // 32px floors so a narrow panel still has a grabbable target), and fully
  // hidden when the pointer leaves the panel. pointermove is listened in
  // CAPTURE phase — the docked viewport isolates bubbling events from
  // LiteGraph.
  const SHOW_CLOSE_BTN = false; // ✕ hidden for now (user request); flip to restore
  let closeState = $state("hidden"); // hidden | dim | full
  function onPanelMove(e) {
    if (!panelEl) return;
    const r = panelEl.getBoundingClientRect();
    const inTop = e.clientY - r.top < Math.max(32, r.height * 0.1);
    const inRight = r.right - e.clientX < Math.max(32, r.width * 0.1);
    closeState = inTop && inRight ? "full" : "dim";
  }
  function onPanelLeave() {
    closeState = "hidden";
  }

  function cleanup() {
    dividerAc?.abort();
    undock(); // never leave the viewport orphaned in a torn-down panel
  }

  onDestroy(() => {
    dividerAc?.abort();
    undock();
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={panelEl}
  class="pcr-pose-panel"
  style:width="{panelWidth}px"
  style:display={isVisible ? "flex" : "none"}
  onpointerdown={(e) => e.stopPropagation()}
  onmousedown={(e) => e.stopPropagation()}
  onclick={(e) => e.stopPropagation()}
  ondblclick={(e) => e.stopPropagation()}
  onpointerleave={onPanelLeave}
  onpointermovecapture={onPanelMove}
>
  <!-- floating close, image-panel style: ghosted while in the panel, full in
       the top-right corner zone, hidden when the pointer is outside -->
  {#if SHOW_CLOSE_BTN}
    <button class="pcr-pose-panel-close-btn" class:pcr-close-dim={closeState === "dim"} class:pcr-close-show={closeState === "full"} title="Close 3D Poser panel" onclick={(e) => {
      e.preventDefault();
      e.stopPropagation();
      hide();
    }}>
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  {/if}

  <!-- the Poser viewport is reparented into here on dock -->
  <div bind:this={bodyEl} class="pcr-pose-panel-body">
    {#if !hasDock}
      <div class="pcr-pose-panel-placeholder">No 3D Poser node in the graph</div>
    {/if}
  </div>
</div>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div bind:this={dividerEl} class="pcr-pose-divider" style:display={isVisible ? "flex" : "none"}
  ondblclick={(e) => {
    e.preventDefault();
    e.stopPropagation();
    const poser = dockedNode || getActivePoser();
    const w = poser?.widgets?.find((x) => x.name === "width")?.value;
    const h = poser?.widgets?.find((x) => x.name === "height")?.value;
    if (!w || !h) return;
    const bodyH = bodyEl?.offsetHeight || panelEl?.offsetHeight || 300;
    panelWidth = clampPanelWidth(Math.round(bodyH * (w / h)));
    if (node.properties) node.properties.pcrPosePanelWidth = panelWidth;
  }}
></div>

<style>
  .pcr-pose-panel {
    display: none;
    flex-direction: column;
    background: rgba(0, 0, 0, 0.4);
    min-width: 200px;
    flex-shrink: 0;
    overflow: hidden;
    position: relative;
  }
  :global(.lg-node-widgets) .pcr-pose-panel {
    background: rgb(0 0 0 / 46%);
  }
  .pcr-pose-panel-close-btn {
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 5; /* above the docked viewport's own corner panels */
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    padding: 0;
    background: transparent;
    border: none;
    color: #fff;
    filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.9)) drop-shadow(0 0 3px rgba(0, 0, 0, 0.7));
    cursor: pointer;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s;
  }
  .pcr-pose-panel-close-btn.pcr-close-dim {
    opacity: 0.3;
    pointer-events: auto;
  }
  .pcr-pose-panel-close-btn.pcr-close-show {
    opacity: 1;
    pointer-events: auto;
  }
  .pcr-pose-panel-body {
    flex: 1;
    min-height: 0;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--pcr-fs-editor-surface, transparent);
  }
  .pcr-pose-panel-placeholder {
    color: rgba(255, 255, 255, 0.3);
    font-size: 12px;
    font-style: italic;
    text-align: center;
    padding: 0 12px;
    user-select: none;
  }
  .pcr-pose-divider {
    width: 4px;
    background: #242424;
    cursor: col-resize;
    flex-shrink: 0;
    display: none;
    align-items: center;
    justify-content: center;
  }
  .pcr-pose-divider::before {
    content: '';
    width: 2px;
    height: 40px;
    background: rgba(255, 255, 255, 0.15);
    border-radius: 1px;
    transition: background 0.15s;
  }
  .pcr-pose-divider:hover::before {
    background: rgba(255, 255, 255, 0.3);
  }
</style>
