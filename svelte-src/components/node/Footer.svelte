<script>
  // Footer — status bar with model indicator, error count, tags dropdown,
  // pos/neg cursor indicator, and word count.

  import { onMount } from "svelte";

  const NEGATIVE_MARKER = "Negative Prompt:";

  let {
    node,
    shared = {},
    // imperative DOM elements passed from main.js (Phase 1)
    modelIndicatorEl = null,
    tagsDropdownEl = null,
    // editor access
    getEditorView = () => null,
    // registration callback — passes API object to parent
    onRegister = null,
  } = $props();

  let leftSlot;
  let rightSlot;

  onMount(() => {
    onRegister?.({ updateWordCount, updatePosNeg, updateErrors, setFocused });
  });
  let editorFocused = $state(false);
  let wordCount = $state(0);
  let errorCount = $state(0);
  let inNegative = $state(false);

  // mount imperative elements into their slots
  $effect(() => {
    if (modelIndicatorEl && leftSlot && !leftSlot.contains(modelIndicatorEl)) {
      leftSlot.prepend(modelIndicatorEl);
    }
  });

  $effect(() => {
    if (tagsDropdownEl && rightSlot && !rightSlot.contains(tagsDropdownEl)) {
      rightSlot.prepend(tagsDropdownEl);
    }
  });

  function countWords(text) {
    let cleaned = text;
    cleaned = cleaned.replace(/Negative Prompt:[\s\S]*$/i, "");
    cleaned = cleaned.replace(/<script[\s\S]*?<\/script>/gi, "");
    cleaned = cleaned.replace(/\/\*[\s\S]*?\*\//g, "");
    cleaned = cleaned.replace(/^\s*\/\/.*$/gm, "");
    cleaned = cleaned.replace(/\s+\/\/.*$/gm, "");
    cleaned = cleaned.replace(/^\s*#.*$/gm, "");
    const words = cleaned.trim().split(/\s+/).filter(w => w.length > 0);
    return words.length;
  }

  function findOptionContext(text, cursorPos) {
    const labelRe = /^::[^:\n]+::/gm;
    const starts = [];
    let m;
    while ((m = labelRe.exec(text)) !== null) starts.push(m.index);

    let optionStart = 0;
    let optionEnd = text.length;
    for (let i = 0; i < starts.length; i++) {
      if (starts[i] <= cursorPos) {
        optionStart = starts[i];
        optionEnd = (i + 1 < starts.length) ? starts[i + 1] : text.length;
      }
    }
    if (starts.length > 0 && cursorPos < starts[0]) {
      optionStart = 0;
      optionEnd = starts[0];
    }

    const negIdx = text.toLowerCase().indexOf(NEGATIVE_MARKER.toLowerCase(), optionStart);
    const negMarker = (negIdx !== -1 && negIdx < optionEnd) ? negIdx : -1;
    return { optionStart, optionEnd, negMarker, hasLabels: starts.length > 0 };
  }

  export function updateWordCount() {
    const view = getEditorView();
    if (!view) return;
    wordCount = countWords(view.state.doc.toString());
  }

  export function updatePosNeg() {
    const view = getEditorView();
    if (!view || !editorFocused) {
      inNegative = false;
      return;
    }
    const pos = view.state.selection.main.head;
    const text = view.state.doc.toString();
    const { negMarker } = findOptionContext(text, pos);
    inNegative = negMarker !== -1 && pos >= negMarker;
  }

  export function updateErrors() {
    const view = getEditorView();
    if (!view) { errorCount = 0; return; }
    errorCount = view.dom.querySelectorAll(".cm-lintRange-error").length;
  }

  export function setFocused(focused) {
    editorFocused = focused;
    updatePosNeg();
  }

  function jumpToPos(e) {
    e.stopPropagation();
    const view = getEditorView();
    if (!view) return;
    const text = view.state.doc.toString();
    const cursorPos = view.state.selection.main.head;
    const { optionStart, optionEnd, negMarker } = findOptionContext(text, cursorPos);

    let jumpPos = optionStart;
    const scanEnd = negMarker !== -1 ? negMarker : optionEnd;
    for (let i = scanEnd - 1; i >= optionStart; i--) {
      if (!/\s/.test(text[i])) { jumpPos = i + 1; break; }
    }
    view.dispatch({ selection: { anchor: jumpPos } });
    view.focus();
    updatePosNeg();
  }

  function jumpToNeg(e) {
    e.stopPropagation();
    const view = getEditorView();
    if (!view) return;
    const text = view.state.doc.toString();
    const cursorPos = view.state.selection.main.head;
    const { optionStart, optionEnd, negMarker, hasLabels } = findOptionContext(text, cursorPos);

    if (negMarker === -1) {
      let lastContent = optionEnd;
      for (let i = optionEnd - 1; i >= optionStart; i--) {
        if (!/\s/.test(text[i])) { lastContent = i + 1; break; }
        if (i === optionStart) lastContent = optionStart;
      }
      const insert = hasLabels
        ? " " + NEGATIVE_MARKER + " "
        : "\n\n" + NEGATIVE_MARKER + "\n";
      view.dispatch({
        changes: { from: lastContent, to: optionEnd, insert },
        selection: { anchor: lastContent + insert.length },
      });
    } else {
      let jumpPos = optionEnd;
      for (let i = optionEnd - 1; i >= negMarker; i--) {
        if (!/\s/.test(text[i])) { jumpPos = i + 1; break; }
      }
      view.dispatch({ selection: { anchor: jumpPos } });
    }
    view.focus();
    updatePosNeg();
  }
</script>

<div class="pcr-footer">
  <div class="pcr-footer-left" bind:this={leftSlot}>
    {#if errorCount > 0}
      <span class="pcr-footer-errors" style="display:inline-flex">
        <span class="pcr-footer-error-icon">{"\u26A0"}</span>
        <span class="pcr-footer-error-count">{errorCount}</span>
      </span>
    {/if}
  </div>

  <div class="pcr-footer-right" bind:this={rightSlot}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <span class="pcr-footer-posneg">
      <span
        class={editorFocused && !inNegative ? "pcr-footer-pos-active" : "pcr-footer-pos-inactive"}
        title="Jump to end of positive section"
        onclick={jumpToPos}
      >Pos</span>
      <span
        class={editorFocused && inNegative ? "pcr-footer-neg-active" : "pcr-footer-neg-inactive"}
        title="Jump to negative section (creates if missing)"
        onclick={jumpToNeg}
      >Neg</span>
    </span>
    <span class="pcr-footer-wordcount">Words {wordCount}</span>
  </div>
</div>

<style>
  .pcr-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0;
    height: 30px;
    background: rgb(0 0 0 / 60%);
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 0 0 3px 3px;
    font-size: 13px;
    color: rgba(255, 255, 255, 0.4);
    user-select: none;
    flex-shrink: 0;
    cursor: default;
  }
  :global(.lg-node-widgets) .pcr-footer {
    background: rgb(0 0 0 / 55%);
  }
  .pcr-footer-left {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1 1 0;
    min-width: 0;
    height: 100%;
    overflow: hidden;
  }
  .pcr-footer-right {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
    height: 100%;
  }
  .pcr-footer-errors {
    display: flex;
    align-items: center;
    gap: 3px;
    height: 100%;
  }
  .pcr-footer-error-icon {
    color: #f0ad4e;
    font-size: 12px;
  }
  .pcr-footer-error-count {
    color: #e74c3c;
    font-weight: bold;
  }
  .pcr-footer-posneg {
    display: flex;
    align-items: center;
    gap: 0;
    height: 100%;
    flex-shrink: 0;
  }
  .pcr-footer-posneg span {
    cursor: pointer;
    transition: color 0.15s;
    height: 100%;
    display: flex;
    align-items: center;
    padding: 0 4px;
  }
  .pcr-footer-pos-active { color: #4fc3f7; }
  .pcr-footer-pos-inactive { color: rgba(255, 255, 255, 0.5); }
  .pcr-footer-pos-inactive:hover { color: #4fc3f7; }
  .pcr-footer-neg-active { color: #e74c3c; }
  .pcr-footer-neg-inactive { color: rgba(255, 255, 255, 0.5); }
  .pcr-footer-neg-inactive:hover { color: #e74c3c; }
  .pcr-footer-wordcount {
    color: rgba(255, 255, 255, 0.5);
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.15s;
    height: 100%;
    display: flex;
    align-items: center;
    flex-shrink: 0;
    padding-right: 10px;
  }
  .pcr-footer-wordcount:hover {
    color: rgba(255, 255, 255, 0.7);
  }
</style>
