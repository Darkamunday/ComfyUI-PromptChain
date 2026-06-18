// LiteGraph event-capture rules learned the hard way:
// 1. Pointer events isolated in bubble phase, not capture — capture-phase
//    blocking breaks LiteGraph's own scrubbing inside the canvas
// 2. Never block pointermove/mousemove — kills text selection in the editor
// 3. onNodeSelected is hooked to immediately undo selections that originated
//    inside our widget (LiteGraph selects on click regardless of stopPropagation)
// 4. Selection clearing is triple-fired (sync + microtask + RAF) — LiteGraph
//    re-applies link highlighting in a deferred RAF that we'd otherwise miss
// 5. Wheel uses window capture phase to beat TransformPane's @wheel.capture
//    in the 2.0 frontend

import { app } from "../../../scripts/app.js";
import { CONFIG, NODE_TYPE } from "./config.js";
import { closeActivePopup } from "./popup-menu.js";

// -- Single global wheel handler for all PromptChain editors -----------------
// Registered once, checks if target is inside any tracked container.
const wheelContainers = new Set();
let wheelListenerInstalled = false;

function installGlobalWheelHandler() {
  if (wheelListenerInstalled) return;
  wheelListenerInstalled = true;

  window.addEventListener("wheel", (event) => {
    // Let autocomplete dropdown handle its own scrolling natively
    const autocomplete = event.target.closest(".cm-tooltip-autocomplete");
    if (autocomplete) {
      event.stopPropagation();
      event.stopImmediatePropagation();
      return;
    }

    // fullscreen overlay handles its own wheel routing
    if (event.target.closest(".pcr-fs-overlay")) return;

    // A docked 3D Poser viewport owns its wheel (camera dolly) via its own
    // window-capture handler. Once docked, its container is a descendant of this
    // node's editor container, so the loop below would match and the editor would
    // swallow the scroll. Defer WITHOUT stopping so the Poser's dolly still fires.
    if (event.target.closest(".pcr-pose-studio")) return;

    let container = null;
    for (const c of wheelContainers) {
      if (c.contains(event.target)) { container = c; break; }
    }
    if (!container) return;

    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();

    // image panel: dispatch zoom event instead of scroll/font-size
    const imagePanel = event.target.closest(".pcr-image-panel");
    if (imagePanel) {
      const rect = imagePanel.getBoundingClientRect();
      imagePanel.dispatchEvent(new CustomEvent("pcr-zoom", {
        detail: {
          deltaY: event.deltaY,
          mouseX: event.clientX - rect.left,
          mouseY: event.clientY - rect.top,
          containerWidth: rect.width,
          containerHeight: rect.height,
        },
      }));
      return;
    }

    if (event.ctrlKey) {
      const delta = event.deltaY > 0 ? -1 : 1;
      // Ctrl+scroll over generated gallery: zoom thumbnails
      const generatedPanel = event.target.closest(".pcr-output-panel-generated");
      if (generatedPanel) {
        const outputPanel = event.target.closest(".pcr-output-panel");
        outputPanel?._updateGalleryZoom?.(delta);
        return;
      }
      // Ctrl+scroll over output panel content or console log: resize output font
      if (event.target.closest(".pcr-output-panel-content, .pcr-console-log")) {
        container._pcrUpdateOutputFontSize(container._pcrOutputFontSize + delta);
        return;
      }
      container._pcrUpdateFontSize(container._pcrFontSize + delta);
      return;
    }

    // scroll isolation — prevent LiteGraph canvas zoom, respect scroll boundaries
    const scroller = event.target.closest(".cm-scroller, .pcr-scrollable, .pcr-ai-panel-body") || container.querySelector(".cm-scroller");
    if (scroller) {
      const atTop = scroller.scrollTop <= 0;
      const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1;
      if (!(atTop && event.deltaY < 0) && !(atBottom && event.deltaY > 0)) {
        scroller.scrollTop += event.deltaY;
        scroller.scrollLeft += event.deltaX;
      }
    }
  }, { capture: true, passive: false });
}

// -- Element-level isolation --------------------------------------------------
// Blocks events from bubbling to LiteGraph. Sets up font size controls.

export function isolateEvents(container, node) {
  const stopEvent = (event) => event.stopPropagation();
  // Svelte 5 delegates click/dblclick/pointerdown/pointerup to the document —
  // stopping them here kills all onclick handlers inside the node widget.
  // Pointer isolation is handled per-component (OutputPanel, ImagePanel, etc.)
  // and installGlobalIsolation handles node selection suppression.
  for (const eventType of [
    "keydown", "keyup", "keypress",
    "mousedown", "mouseup",
    "copy", "paste", "cut",
  ]) {
    container.addEventListener(eventType, stopEvent);
  }

  // close popups on interaction inside the isolated container,
  // since stopPropagation prevents document-level dismiss handlers from firing
  container.addEventListener("pointerdown", () => closeActivePopup());

  // font size state (persisted to node.properties.pcrFontSize)
  container._pcrFontSize = node?.properties?.pcrFontSize || CONFIG.defaultFontSize;
  container._pcrUpdateFontSize = (size) => {
    size = Math.max(CONFIG.minFontSize, Math.min(CONFIG.maxFontSize, size));
    container._pcrFontSize = size;
    container.style.setProperty("--pcr-font-size", `${size}px`);
    if (node) {
      if (!node.properties) node.properties = {};
      node.properties.pcrFontSize = size;
    }
  };
  container.style.setProperty("--pcr-font-size", `${container._pcrFontSize}px`);

  // output panel font size — independent from editor
  container._pcrOutputFontSize = node?.properties?.pcrOutputFontSize || CONFIG.defaultFontSize;
  container._pcrUpdateOutputFontSize = (size) => {
    size = Math.max(CONFIG.minFontSize, Math.min(CONFIG.maxFontSize, size));
    container._pcrOutputFontSize = size;
    container.style.setProperty("--pcr-output-font-size", `${size}px`);
    if (node) {
      if (!node.properties) node.properties = {};
      node.properties.pcrOutputFontSize = size;
    }
  };
  container.style.setProperty("--pcr-output-font-size", `${container._pcrOutputFontSize}px`);

  // register for global wheel handler
  wheelContainers.add(container);
  installGlobalWheelHandler();

  // release pointer capture that LiteGraph sets on its canvas
  container.addEventListener("pointerdown", (event) => {
    const canvas = document.querySelector("canvas.lgraphcanvas");
    if (canvas && typeof event.pointerId === "number") {
      try {
        if (canvas.hasPointerCapture(event.pointerId)) {
          canvas.releasePointerCapture(event.pointerId);
        }
      } catch {} // capture may already be released
    }
  });

  // allow middle-click canvas panning through our widget
  container.addEventListener("pointerdown", (event) => {
    if (event.button === 1) app.canvas?.processMouseDown(event);
  });
  container.addEventListener("pointermove", (event) => {
    if ((event.buttons & 4) === 4) app.canvas?.processMouseMove(event);
  });

  // forward pointerup to the canvas when a link drag is in progress —
  // the DOM widget sits on top of the canvas, so pointerup never reaches
  // LiteGraph's processMouseUp, which means dropLinks never fires and
  // connections silently fail when dropped over the widget area.
  container.addEventListener("pointerup", (event) => {
    if (app.canvas?.linkConnector?.isConnecting) {
      app.canvas.processMouseUp(event);
    }
  });
}

// Remove a container from wheel tracking (call on node removal).
export function removeWheelContainer(container) {
  wheelContainers.delete(container);
}

// -- Global node selection prevention (1.0 only) -----------------------------
// In 1.0, LiteGraph selects nodes on click regardless of stopPropagation.
// We hook onNodeSelected and immediately undo the selection if the click
// originated inside our editor. Uses triple-clear to beat LiteGraph's
// deferred link highlighting.

let clickedNodeId = null;
let globalIsolationInstalled = false;

// Clears selection at three timing points to catch all LiteGraph highlighting.
function clearSelectionAndHighlights() {
  const clear = () => {
    app.canvas?.deselectAll();
    if (app.canvas) app.canvas.highlighted_links = {};
    app.canvas?.setDirty(true, true);
  };
  clear();
  Promise.resolve().then(clear);
  requestAnimationFrame(clear);
}

export function installGlobalIsolation() {
  if (globalIsolationInstalled) return;
  if (!app.canvas) return;
  globalIsolationInstalled = true;

  // track clicks inside our UI (capture phase fires before LiteGraph)
  window.addEventListener("pointerdown", (event) => {
    const inEditor = event.target.closest(".pcr-editor, .cm-editor, .cm-content, .cm-line, .cm-scroller");
    if (inEditor) {
      const nodeElement = event.target.closest("[data-node-id]");
      clickedNodeId = nodeElement ? parseInt(nodeElement.dataset.nodeId, 10) : findOwnerNodeId(event.target);
    } else {
      clickedNodeId = null;
    }
  }, true);

  document.addEventListener("pointerup", () => {
    requestAnimationFrame(() => { clickedNodeId = null; });
  }, true);

  // undo selection when the click was inside our editor
  const originalOnNodeSelected = app.canvas.onNodeSelected;
  app.canvas.onNodeSelected = function (node) {
    if (node?.comfyClass === NODE_TYPE && clickedNodeId === node.id) {
      clickedNodeId = null;
      clearSelectionAndHighlights();
      return;
    }
    clickedNodeId = null;
    return originalOnNodeSelected?.call(this, node);
  };
}

// Walks all graph nodes to find which PromptChain node owns a DOM element.
function findOwnerNodeId(element) {
  if (!app.graph?._nodes) return null;
  for (const node of app.graph._nodes) {
    if (node.comfyClass !== NODE_TYPE) continue;
    const widget = node.widgets?.find((w) => w.name === "pcr_editor");
    if (widget?.element?.contains(element)) return node.id;
  }
  return null;
}
