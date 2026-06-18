// Wildcard Badge — CM6 extension that renders inline mode badges after __wildcard__ tokens.
// Clicking a badge opens the wildcard mode dropdown popup.

import { showWildcardModePopup, getWildcardMode, MODE_EMOJI } from "./wildcard-dropdown.js";

const WC_PATTERN = /__([a-zA-Z0-9_/.-]+)__/g;

/**
 * Creates the wildcard badge CM6 extension.
 * Returns { extension, setNode } where setNode(node) binds the extension to a graph node.
 */
export function createWildcardBadgeExtension(CM) {
  // State: which node and view this editor belongs to
  let currentNode = null;
  let currentView = null;

  // Callback when a mode changes (set externally)
  let onModeChanged = null;

  class WildcardBadgeWidget extends CM.WidgetType {
    constructor(name, mode, label, rolledLabel) {
      super();
      this.name = name;
      this.mode = mode;
      this.label = label;
      this.rolledLabel = rolledLabel;
    }

    eq(other) {
      return this.name === other.name && this.mode === other.mode
        && this.label === other.label && this.rolledLabel === other.rolledLabel;
    }

    toDOM() {
      const span = document.createElement("span");
      span.className = "pcr-wc-badge";

      if (this.mode === "switch" && this.label) {
        span.classList.add("pcr-wc-badge--switch");
        span.textContent = `\u2705 ${this.label}`;
      } else if (this.mode === "combine") {
        span.classList.add("pcr-wc-badge--combine");
        span.textContent = `${MODE_EMOJI.combine} all`;
      } else if (this.mode === "iterate") {
        span.classList.add("pcr-wc-badge--iterate");
        span.textContent = `${MODE_EMOJI.iterate}`;
      } else if (this.mode === "none") {
        span.classList.add("pcr-wc-badge--none");
        span.textContent = `${MODE_EMOJI.none} off`;
      } else if (this.rolledLabel) {
        span.classList.add("pcr-wc-badge--randomize");
        span.textContent = `${MODE_EMOJI.randomize} ${this.rolledLabel}`;
      } else {
        span.classList.add("pcr-wc-badge--randomize");
        span.textContent = MODE_EMOJI.randomize;
      }

      span.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();
      });
      const wcName = this.name;
      span.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!currentNode) return;
        const rect = span.getBoundingClientRect();
        showWildcardModePopup(currentNode, wcName, rect, () => {
          onModeChanged?.();
          // place cursor right before the badge (end of __name__ token)
          if (currentView) {
            const doc = currentView.state.doc.toString();
            const token = `__${wcName}__`;
            const idx = doc.indexOf(token);
            if (idx >= 0) {
              const pos = idx + token.length;
              currentView.dispatch({ selection: { anchor: pos } });
              currentView.focus();
            }
          }
        });
      });

      return span;
    }

    ignoreEvent() { return false; }
  }

  // Effect + field to force badge rebuild (dispatched after mode changes)
  const refreshEffect = CM.StateEffect.define();

  const refreshField = CM.StateField.define({
    create: () => 0,
    update(value, tr) {
      for (const e of tr.effects) {
        if (e.is(refreshEffect)) return value + 1;
      }
      return value;
    },
  });

  // ViewPlugin that scans visible ranges for __name__ and adds widget decorations
  const badgePlugin = CM.ViewPlugin.fromClass(class {
    constructor(view) {
      currentView = view;
      this.decorations = this.build(view);
    }

    update(update) {
      const prevV = update.startState.field(refreshField);
      const currV = update.state.field(refreshField);
      if (update.docChanged || update.viewportChanged || prevV !== currV) {
        this.decorations = this.build(update.view);
      }
    }

    build(view) {
      const decos = [];
      for (const { from, to } of view.visibleRanges) {
        const text = view.state.sliceDoc(from, to);
        WC_PATTERN.lastIndex = 0;
        let match;
        while ((match = WC_PATTERN.exec(text)) !== null) {
          const name = match[1];
          const wcMode = currentNode ? getWildcardMode(currentNode, name) : { mode: "randomize", index: 0 };
          const wcResult = currentNode?.properties?.pcrWildcardResults?.[name];
          const rolledLabel = (wcMode.mode === "randomize" || !wcMode.mode) && wcResult?.label ? wcResult.label : "";
          const widget = new WildcardBadgeWidget(name, wcMode.mode, wcMode.label || "", rolledLabel);
          const pos = from + match.index + match[0].length; // after the closing __
          decos.push(CM.Decoration.widget({ widget, side: -1 }).range(pos));
        }
      }
      return CM.Decoration.set(decos, true);
    }
  }, { decorations: v => v.decorations });

  function setNode(node) {
    currentNode = node;
  }

  function setOnModeChanged(cb) {
    onModeChanged = cb;
  }

  function refreshBadges(view) {
    if (view) {
      currentView = view;
      view.dispatch({ effects: refreshEffect.of(null) });
    }
  }

  return {
    extension: [refreshField, badgePlugin],
    setNode,
    setOnModeChanged,
    refreshBadges,
  };
}
