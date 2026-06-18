// Active Label Highlight — highlights the selected ::Label:: line in switch mode.
// Self-contained CM6 extension. No node/app imports.

// Returns the 1-based label index at a given 0-indexed line number, or -1.
export function getLabelIndexAtLine(doc, lineNum) {
  let labelCount = 0;
  for (let i = 1; i <= doc.lines; i++) {
    const trimmed = doc.line(i).text.trim();
    if (trimmed.startsWith("//") || trimmed.startsWith("#")) continue;
    if (/^::([^:]+)::/.test(trimmed)) {
      labelCount++;
      if (i - 1 === lineNum) return labelCount;
    }
  }
  return -1;
}

export function createHighlightExtension(CM) {
  const setHighlightEffect = CM.StateEffect.define();
  const switchLineDeco = CM.Decoration.line({ class: "pcr-line-active" });
  const iterateLineDeco = CM.Decoration.line({ class: "pcr-line-active pcr-line-iterate" });

  const highlightField = CM.StateField.define({
    create: () => ({ mode: "combine", switchIndex: 0 }),
    update(value, tr) {
      for (const e of tr.effects) {
        if (e.is(setHighlightEffect)) return e.value;
      }
      return value;
    },
    // container class (pcr-switch-active / pcr-iterate-active) is managed
    // by the plugin below — it checks for actual ::Label:: lines first
    provide: () => [],
  });

  const highlightPlugin = CM.ViewPlugin.fromClass(class {
    constructor(view) {
      this.decorations = this.build(view.state);
      this._syncContainerClass(view);
    }
    update(update) {
      const prev = update.startState.field(highlightField);
      const curr = update.state.field(highlightField);
      if (update.docChanged || prev !== curr) {
        this.decorations = this.build(update.state);
        this._syncContainerClass(update.view);
      }
    }
    build(state) {
      const { mode, switchIndex } = state.field(highlightField);
      if ((mode !== "switch" && mode !== "iterate") || switchIndex < 1) return CM.Decoration.none;

      const doc = state.doc;
      let labelCount = 0;
      for (let i = 1; i <= doc.lines; i++) {
        const line = doc.line(i);
        const trimmed = line.text.trim();
        if (trimmed.startsWith("//") || trimmed.startsWith("#")) continue;
        if (/^::([^:]+)::/.test(trimmed)) {
          labelCount++;
          if (labelCount === switchIndex) {
            const deco = mode === "iterate" ? iterateLineDeco : switchLineDeco;
            const decos = [deco.range(line.from)];
            // include continuation lines until next label or end
            for (let j = i + 1; j <= doc.lines; j++) {
              const cont = doc.line(j).text.trim();
              if (/^::([^:]+)::/.test(cont)) break;
              if (cont) decos.push(deco.range(doc.line(j).from));
            }
            return CM.Decoration.set(decos);
          }
        }
      }
      return CM.Decoration.none;
    }
    // only apply the dimming container class when labels actually exist
    _syncContainerClass(view) {
      const el = view.dom.closest(".pcr-editor");
      if (!el) return;
      const hasDecorations = this.decorations.size > 0;
      el.classList.toggle("pcr-switch-active", hasDecorations && view.state.field(highlightField).mode === "switch");
      el.classList.toggle("pcr-iterate-active", hasDecorations && view.state.field(highlightField).mode === "iterate");
    }
  }, { decorations: v => v.decorations });

  function setHighlightState(view, state) {
    view.dispatch({ effects: setHighlightEffect.of(state) });
  }

  return { extension: [highlightField, highlightPlugin], setHighlightState };
}
