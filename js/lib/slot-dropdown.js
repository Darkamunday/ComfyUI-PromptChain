// Slot dropdown — cascading dropdown on input slot labels.
// Clicking a ⏷ label opens a popup to control the connected child's mode.
// Supports n-level nesting: if the selected grandchild has options, cascade deeper.

import { app } from "../../../scripts/app.js";
import { isVueMode } from "./config.js";
import { hasCustomTitle, getConnectedInputs, hasOptions } from "./slot-utils.js";
import { getSwitchOptions } from "./label-utils.js";
import { closeActivePopup, isPopupOpen, createSearchableList, openPopup } from "./popup-menu.js";

// Show the slot dropdown popup for a child node.
// parentNode = the node whose input label was clicked.
// childNode = the connected child node whose mode we're controlling.
// triggerRect = bounding rect of the clicked label element.
// onChanged = callback after mode/selection changes.
function showSlotDropdown(childNode, parentNode, triggerRect, onChanged, quickSelect) {
  const popupKey = quickSelect
    ? `${childNode.id}_qs${quickSelect.slotIndex}`
    : `${childNode.id}`;
  if (isPopupOpen(popupKey)) {
    closeActivePopup();
    return;
  }

  const currentMode = childNode.properties?.pcrMode || "switch";
  const currentSwitchIndex = childNode.properties?.pcrSwitchIndex ?? 1;
  const options = getSwitchOptions(childNode);
  const hasMultiple = options.length > 1;

  const menu = document.createElement("div");
  menu.className = "pcr-mode-menu";

  let close; // forward reference — set by openPopup below

  // -- mode section --
  const modeSection = document.createElement("div");
  modeSection.className = "pcr-mode-menu-modes";

  function createModeItem(label, mode, disabled) {
    const item = document.createElement("div");
    item.className = "pcr-mode-menu-item pcr-mode-menu-mode-option";
    if (disabled) item.classList.add("pcr-mode-menu-disabled");
    if (currentMode === mode) item.classList.add("pcr-mode-menu-selected");

    const text = document.createElement("span");
    text.textContent = label;
    item.appendChild(text);

    if (currentMode === mode) {
      const check = document.createElement("span");
      check.className = "pcr-mode-menu-check";
      check.textContent = "\u2713";
      item.appendChild(check);
    }

    if (!disabled) {
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        setChildMode(childNode, mode);
        close();
        onChanged?.();
      });
    }
    return item;
  }

  // quick-select option (first item when invoked from parent input label)
  if (quickSelect) {
    const qsItem = document.createElement("div");
    qsItem.className = "pcr-mode-menu-item pcr-mode-menu-mode-option";
    const isSelected = currentMode === "switch" && currentSwitchIndex === quickSelect.slotIndex;
    if (isSelected) qsItem.classList.add("pcr-mode-menu-selected");
    qsItem.innerHTML = `<span>\u2705 Select: ${quickSelect.childName}</span>`;
    if (isSelected) qsItem.innerHTML += `<span class="pcr-mode-menu-check">\u2713</span>`;
    qsItem.addEventListener("click", (e) => {
      e.stopPropagation();
      setChildMode(childNode, "switch", quickSelect.slotIndex);
      close();
      onChanged?.();
    });
    modeSection.appendChild(qsItem);
  }

  modeSection.appendChild(createModeItem("\uD83C\uDFB2 Randomize", "roll", !hasMultiple));
  modeSection.appendChild(createModeItem("\uD83D\uDCDA Combine", "combine", !hasMultiple));

  // iterate option with reset button
  const iterateItem = createModeItem("\u267B\uFE0F Iterate", "iterate", !hasMultiple);
  if (currentMode === "iterate") {
    const resetBtn = document.createElement("span");
    resetBtn.className = "pcr-mode-menu-reset";
    resetBtn.textContent = "\u21BA";
    resetBtn.title = "Reset iterate position";
    resetBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!childNode.properties) childNode.properties = {};
      childNode.properties.pcrIterateIndex = 0;
      childNode.properties.pcrIterateCurrent = 0;
      childNode.properties.pcrIterateCycle = 1;
      childNode.properties.pcrIterateTotal = 0;
      childNode.properties.pcrIteratePending = true;
      const hash = childNode.properties.pcrIterateContentHash;
      if (hash) {
        fetch("/promptchain/iterate/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content_hash: hash }),
        }).catch(() => {});
      }
      childNode._pcrMenubar?.updateModeDisplay?.();
      close();
      onChanged?.();
    });
    iterateItem.appendChild(resetBtn);
  }
  modeSection.appendChild(iterateItem);

  // None option
  const noneItem = document.createElement("div");
  noneItem.className = "pcr-mode-menu-item pcr-mode-menu-mode-option";
  if (currentMode === "switch" && currentSwitchIndex === 0) noneItem.classList.add("pcr-mode-menu-selected");
  noneItem.innerHTML = `<span>\u274C None</span>`;
  if (currentMode === "switch" && currentSwitchIndex === 0) {
    noneItem.innerHTML += `<span class="pcr-mode-menu-check">\u2713</span>`;
  }
  noneItem.addEventListener("click", (e) => {
    e.stopPropagation();
    setChildMode(childNode, "switch", 0);
    close();
    onChanged?.();
  });
  modeSection.appendChild(noneItem);
  menu.appendChild(modeSection);

  // -- searchable options list --
  const list = createSearchableList({
    options,
    onSelect: (opt) => {
      setChildMode(childNode, "switch", opt.index);
      close();

      const canCascade = opt.sourceNode && hasOptions(opt.sourceNode);
      if (canCascade && parentNode) {
        const cascadeRect = triggerRect;
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            showSlotDropdown(opt.sourceNode, childNode, cascadeRect, onChanged);
          });
        });
      }

      onChanged?.();
    },
    currentMode,
    currentSwitchIndex,
    itemPrefix: "\uD83D\uDFE2 ",
    extraItemSetup: (item, opt) => {
      const canCascade = opt.sourceNode && hasOptions(opt.sourceNode);
      if (canCascade) {
        const arrow = document.createElement("span");
        arrow.style.cssText = "margin-left:4px;color:#666;font-size:10px;";
        arrow.textContent = "\u25B8";
        item.appendChild(arrow);
      }
    },
  });

  if (list.searchContainer) menu.appendChild(list.searchContainer);
  if (list.separator) menu.appendChild(list.separator);
  menu.appendChild(list.listContainer);

  close = openPopup(menu, triggerRect, popupKey);

  list.renderList();
  if (list.searchInput) requestAnimationFrame(() => list.searchInput.focus());
}

function setChildMode(childNode, mode, switchIndex) {
  if (!childNode.properties) childNode.properties = {};
  childNode.properties.pcrMode = mode;
  if (switchIndex !== undefined) childNode.properties.pcrSwitchIndex = switchIndex;

  const modeWidget = childNode.widgets?.find(w => w.name === "mode");
  if (modeWidget) modeWidget.value = mode;
  if (switchIndex !== undefined) {
    const switchIndexWidget = childNode.widgets?.find(w => w.name === "switch_index");
    if (switchIndexWidget) switchIndexWidget.value = switchIndex;
  }

  // update child's own menubar
  childNode._pcrMenubar?.updateModeDisplay?.();
  app.graph?.setDirtyCanvas?.(true, true);
}

// Attach click handlers to input slot labels in 2.0 mode.
// Rebuilds label DOM with separate clickable spans per chain level.
export function attachSlotClickHandlers(node) {
  if (!node.inputs) return;

  const nodeEl = document.querySelector(`[data-node-id="${node.id}"]`);
  if (!nodeEl) return;

  const slotEls = nodeEl.querySelectorAll(".lg-slot--input");
  const onChanged = () => {
    // refresh all PromptChain nodes' labels — mode change on a child
    // affects labels on this node, intermediate nodes, and grandparents
    if (node._pcrRefreshAllLabels) node._pcrRefreshAllLabels();
    else node.onConnectionsChange?.();
  };

  const preventSelect = (e) => {
    e.stopPropagation();
    e.stopImmediatePropagation();
    e.preventDefault();
  };

  let slotIdx = 0;
  for (let i = 0; i < node.inputs.length; i++) {
    const slot = node.inputs[i];
    if (!slot.name?.includes("in_")) continue;
    if (slot.link == null) { slotIdx++; continue; }

    const chain = slot._pcrOrderChain || [];
    const source = slot._pcrSourceNode;
    if (!source) { slotIdx++; continue; }

    const slotEl = slotEls[slotIdx];
    if (!slotEl) { slotIdx++; continue; }

    const labelEl = slotEl.querySelector("span");
    if (!labelEl) { slotIdx++; continue; }

    // clean up previous handlers
    if (labelEl._pcrOrderSpans) {
      for (const entry of labelEl._pcrOrderSpans) {
        entry.span.removeEventListener("click", entry.handler);
      }
    }
    labelEl._pcrOrderSpans = [];

    // get mode icon from parent
    const connectedCount = getConnectedInputs(node).length;
    const parentMode = node.properties?.pcrMode || "switch";
    const slotName = slot.name.replace("inputs.", "");
    const slotIndex = parseInt(slotName.split("_")[1]) + 1; // 1-based
    let modeIcon = "";
    if (parentMode === "switch") {
      const switchIndex = node.properties?.pcrSwitchIndex ?? 1;
      modeIcon = (switchIndex === slotIndex) ? "✅ " : "❌ ";
    } else if (connectedCount >= 2) {
      if (parentMode === "combine") modeIcon = "📚 ";
      else if (parentMode === "roll") modeIcon = "🎲 ";
      else if (parentMode === "iterate") modeIcon = "♻️ ";
    }

    const childName = hasCustomTitle(source) ? source.title : "PromptChain";

    // dim non-selected/non-winning slots
    const switchIndex = node.properties?.pcrSwitchIndex ?? 1;
    const rollSelected = node.properties?.pcrRollSelected;
    const isDimmed = (parentMode === "switch" && switchIndex !== 0 && switchIndex !== slotIndex)
      || (parentMode === "roll" && rollSelected >= 1 && rollSelected !== slotIndex);
    slotEl.classList.toggle("pcr-slot-dimmed", isDimmed);

    // rebuild label DOM
    labelEl.textContent = "";

    // parent mode dropdown (icon + child name) — clickable to change parent mode
    const parentDropSpan = document.createElement("span");
    parentDropSpan.className = "pcr-slot-dropdown";

    if (parentMode === "switch") {
      parentDropSpan.classList.add(switchIndex === slotIndex ? "pcr-slot-dropdown-active" : "pcr-slot-dropdown-none");
    } else if (parentMode === "combine") {
      parentDropSpan.classList.add("pcr-slot-dropdown-combine");
    } else if (parentMode === "roll") {
      parentDropSpan.classList.add("pcr-slot-dropdown-roll");
    } else if (parentMode === "iterate") {
      parentDropSpan.classList.add("pcr-slot-dropdown-iterate");
    }

    parentDropSpan.textContent = `${modeIcon}${childName}`;
    const parentArrow = document.createElement("span");
    parentArrow.textContent = "▾";
    parentArrow.className = "pcr-slot-arrow";
    parentDropSpan.appendChild(parentArrow);

    const parentClickHandler = (e) => {
      e.stopPropagation();
      e.stopImmediatePropagation();
      e.preventDefault();
      const rect = parentDropSpan.getBoundingClientRect();
      showSlotDropdown(node, null, rect, onChanged, { slotIndex, childName });
    };
    parentDropSpan.addEventListener("click", parentClickHandler);
    parentDropSpan.addEventListener("mousedown", preventSelect, true);
    parentDropSpan.addEventListener("pointerdown", preventSelect, true);
    parentDropSpan.addEventListener("pointerup", preventSelect, true);
    labelEl._pcrOrderSpans.push({ span: parentDropSpan, handler: parentClickHandler });
    labelEl.appendChild(parentDropSpan);

    // create clickable dropdown span for each chain level
    for (let c = 0; c < chain.length; c++) {
      const chainEntry = chain[c];

      if (c > 0) {
        // spacing between chain levels
        const spacer = document.createElement("span");
        spacer.textContent = " ";
        labelEl.appendChild(spacer);
      }

      const dropSpan = document.createElement("span");
      dropSpan.className = "pcr-slot-dropdown";
      dropSpan.textContent = chainEntry.modeLabel;
      const chainArrow = document.createElement("span");
      chainArrow.textContent = "▾";
      chainArrow.className = "pcr-slot-arrow";
      dropSpan.appendChild(chainArrow);

      // apply mode-specific CSS class
      const childMode = chainEntry.node.properties?.pcrMode || "switch";
      const childSwitch = chainEntry.node.properties?.pcrSwitchIndex ?? 1;
      if (childMode === "switch" && childSwitch > 0) dropSpan.classList.add("pcr-slot-dropdown-active");
      else if (childMode === "switch" && childSwitch === 0) dropSpan.classList.add("pcr-slot-dropdown-none");
      else if (childMode === "combine") dropSpan.classList.add("pcr-slot-dropdown-combine");
      else if (childMode === "roll") dropSpan.classList.add("pcr-slot-dropdown-roll");
      else if (childMode === "iterate") dropSpan.classList.add("pcr-slot-dropdown-iterate");

      labelEl.appendChild(dropSpan);

      // click handler for this specific chain level
      const targetNode = chainEntry.node;
      const clickHandler = (e) => {
        e.stopPropagation();
        e.stopImmediatePropagation();
        e.preventDefault();
        const rect = dropSpan.getBoundingClientRect();
        showSlotDropdown(targetNode, node, rect, onChanged);
      };
      dropSpan.addEventListener("click", clickHandler);
      dropSpan.addEventListener("mousedown", preventSelect, true);
      dropSpan.addEventListener("pointerdown", preventSelect, true);
      dropSpan.addEventListener("pointerup", preventSelect, true);
      labelEl._pcrOrderSpans.push({ span: dropSpan, handler: clickHandler });
    }

    slotIdx++;
  }
}

// Hit-test input slot labels in 1.0 canvas mode.
// Returns the input index whose label is under pos, or -1.
function hitTestSlotLabel(node, pos) {
  if (pos[0] <= 15 || !node.inputs) return -1;
  const ctx = app.canvas?.ctx;
  if (!ctx) return -1;
  for (let i = 0; i < node.inputs.length; i++) {
    const slot = node.inputs[i];
    if (!slot.name?.includes("in_")) continue;
    if (slot.link == null || !slot._pcrSourceNode) continue;
    const slotPos = node.getConnectionPos(true, i);
    const slotLocalY = slotPos[1] - node.pos[1];
    if (Math.abs(pos[1] - slotLocalY) < 10) {
      // check x is within the label text bounds
      const slotLocalX = slotPos[0] - node.pos[0];
      const prevFont = ctx.font;
      ctx.font = "normal 12px Inter";
      const labelW = ctx.measureText(slot.label || "").width;
      ctx.font = prevFont;
      if (pos[0] <= slotLocalX + 10 + labelW + 4) return i;
    }
  }
  return -1;
}

// Decompose a slot label into segments and measure their x positions.
// Segments = [parentPrefix, chain0, chain1, ...] matching 2.0 dropdown spans.
function getSegmentLayout(node, slotIdx, ctx) {
  const slot = node.inputs?.[slotIdx];
  if (!slot?.label || slot.link == null) return null;

  const chain = slot._pcrOrderChain || [];
  const segments = [];
  if (chain.length > 0) {
    const chainLabels = chain.map(c => c.modeLabel);
    const chainStr = chainLabels.join("  ");
    segments.push(slot.label.substring(0, slot.label.length - chainStr.length - 3));
    segments.push(...chainLabels);
  } else {
    segments.push(slot.label);
  }

  const slotPos = node.getConnectionPos(true, slotIdx);
  const localX = slotPos[0] - node.pos[0];
  const localY = slotPos[1] - node.pos[1];

  const prevFont = ctx.font;
  ctx.font = "normal 12px Inter";

  const positions = [];
  let textX = localX + 11;
  for (let s = 0; s < segments.length; s++) {
    const w = ctx.measureText(segments[s]).width;
    positions.push({ x: textX, w });
    textX += w;
    if (s === 0 && segments.length > 1) textX += ctx.measureText("   ").width;
    else if (s < segments.length - 1) textX += ctx.measureText("  ").width;
  }

  ctx.font = prevFont;
  return { positions, localY };
}

// Canvas handlers for 1.0 mode — click opens dropdown, hover draws highlight.
// In 1.0, slots are painted on the canvas (no DOM elements), so we use
// node.onMouseDown/onMouseMove with hit-testing against slot y-positions.
export function attachCanvasClickHandler(node) {
  const origOnMouseDown = node.onMouseDown;
  const origOnMouseMove = node.onMouseMove;
  const origOnMouseLeave = node.onMouseLeave;
  const origOnDrawForeground = node.onDrawForeground;

  node._pcrHoveredSlot = -1;
  node._pcrHoveredSegment = -1;

  // Hover — track which slot and segment the mouse is over
  node.onMouseMove = function (event, pos, graphCanvas) {
    if (!isVueMode()) {
      const hit = hitTestSlotLabel(this, pos);
      let seg = -1;

      if (hit >= 0) {
        const ctx = app.canvas?.ctx;
        if (ctx) {
          const layout = getSegmentLayout(this, hit, ctx);
          if (layout) {
            for (let s = 0; s < layout.positions.length; s++) {
              const p = layout.positions[s];
              if (pos[0] >= p.x - 2 && pos[0] <= p.x + p.w + 2) {
                seg = s;
                break;
              }
            }
          }
        }
        if (seg < 0) seg = 0;
      }

      if (hit !== this._pcrHoveredSlot || seg !== this._pcrHoveredSegment) {
        this._pcrHoveredSlot = hit;
        this._pcrHoveredSegment = seg;
        app.graph?.setDirtyCanvas?.(true, false);
      }
    }
    return origOnMouseMove?.call(this, event, pos, graphCanvas);
  };

  node.onMouseLeave = function (event) {
    if (this._pcrHoveredSlot >= 0) {
      this._pcrHoveredSlot = -1;
      this._pcrHoveredSegment = -1;
      app.graph?.setDirtyCanvas?.(true, false);
    }
    return origOnMouseLeave?.call(this, event);
  };

  // Highlight behind the single hovered segment (runs before drawSlots)
  node.onDrawForeground = function (ctx, graphCanvas) {
    origOnDrawForeground?.call(this, ctx, graphCanvas);
    if (this._pcrHoveredSlot < 0 || this._pcrHoveredSegment < 0) return;

    const layout = getSegmentLayout(this, this._pcrHoveredSlot, ctx);
    if (!layout || this._pcrHoveredSegment >= layout.positions.length) return;

    const p = layout.positions[this._pcrHoveredSegment];
    const pad = 2;

    ctx.save();
    ctx.fillStyle = "#ffffff15";
    ctx.strokeStyle = "#ffffff30";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(p.x - pad, layout.localY - 8, p.w + pad * 2, 18, 4);
    else ctx.rect(p.x - pad, layout.localY - 8, p.w + pad * 2, 18);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  };

  // Click — open slot dropdown for the clicked segment.
  // Segment 0 = parent mode dropdown, segment N = chain[N-1] node dropdown.
  node.onMouseDown = function (event, pos, graphCanvas) {
    if (!isVueMode()) {
      const hit = hitTestSlotLabel(this, pos);
      if (hit >= 0) {
        const slot = this.inputs[hit];

        // determine which segment was clicked
        let seg = 0;
        const ctx = app.canvas?.ctx;
        if (ctx) {
          const layout = getSegmentLayout(this, hit, ctx);
          if (layout) {
            for (let s = 0; s < layout.positions.length; s++) {
              const p = layout.positions[s];
              if (pos[0] >= p.x - 2 && pos[0] <= p.x + p.w + 2) { seg = s; break; }
            }
          }
        }

        const onChanged = () => this.onConnectionsChange?.();

        // build the popup key to check toggle vs switch
        let popupKey, targetNode, quickSelect;
        if (seg === 0) {
          const source = slot._pcrSourceNode;
          const slotName = slot.name.replace("inputs.", "");
          const slotIndex = parseInt(slotName.split("_")[1]) + 1;
          const childName = hasCustomTitle(source) ? source.title : "PromptChain";
          popupKey = `${this.id}_qs${slotIndex}`;
          targetNode = this;
          quickSelect = { slotIndex, childName };
        } else {
          const chain = slot._pcrOrderChain || [];
          if (seg - 1 < chain.length) {
            targetNode = chain[seg - 1].node;
            popupKey = `${targetNode.id}`;
          }
        }

        // toggle: if clicking the same segment that's already open, just close
        if (popupKey && isPopupOpen(popupKey)) {
          closeActivePopup();
          return true;
        }

        // close any other popup, then open the new one
        closeActivePopup();

        if (targetNode) {
          const triggerRect = {
            left: event.clientX - 10,
            right: event.clientX + 10,
            top: event.clientY - 5,
            bottom: event.clientY + 5,
          };
          const self = this;
          requestAnimationFrame(() => {
            if (quickSelect) {
              showSlotDropdown(self, null, triggerRect, onChanged, quickSelect);
            } else {
              showSlotDropdown(targetNode, self, triggerRect, onChanged);
            }
          });
        }
        return true;
      }
    }
    return origOnMouseDown?.call(this, event, pos, graphCanvas);
  };
}
