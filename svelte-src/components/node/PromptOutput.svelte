<script>
  // Compiled prompt text display — positive + negative sections.

  let {
    compiledOutput = "",
    compiledNegOutput = "",
  } = $props();

  const _HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, c => _HTML_ESCAPES[c]);
  }

  let html = $derived.by(() => {
    if (!compiledOutput && !compiledNegOutput) {
      return '<span class="pcr-output-panel-placeholder">Queue workflow to see prompt output</span>';
    }
    let result = `<span class="pcr-output-panel-label-pos">Positive:</span>\n${escapeHtml(compiledOutput)}`;
    if (compiledNegOutput) {
      result += `\n\n<span class="pcr-output-panel-label-neg">Negative:</span>\n${escapeHtml(compiledNegOutput)}`;
    }
    return result;
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  class="pcr-output-panel-content pcr-scrollable"
  tabindex="0"
  onpointerdown={(e) => e.stopPropagation()}
  onmousedown={(e) => e.stopPropagation()}
>
  {@html html}
</div>

<style>
  .pcr-output-panel-content {
    flex: 1 1 0;
    overflow-y: auto !important;
    overflow-x: hidden;
    min-height: 0;
    padding: 8px 12px;
    font-family: Consolas, Menlo, "Liberation Mono", monospace;
    font-size: var(--pcr-output-font-size, 13px);
    color: rgba(255, 255, 255, 0.85);
    cursor: text;
    user-select: text;
    -webkit-user-select: text;
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.5;
    background: repeating-linear-gradient(45deg, transparent, transparent 10px, #cccccc03 10px, #cccccc03 20px);
    outline: none;
  }
  .pcr-output-panel-content:focus { outline: none; }
  /* {@html} content — needs :global */
  :global(.pcr-output-panel-label-pos) { color: #4fc3f7; font-weight: 600; }
  :global(.pcr-output-panel-label-neg) { color: #e74c3c; font-weight: 600; }
  :global(.pcr-output-panel-placeholder) {
    color: rgba(255, 255, 255, 0.4);
    font-style: italic;
  }
  .pcr-output-panel-content::-webkit-scrollbar { width: 6px; }
  .pcr-output-panel-content::-webkit-scrollbar-track { background: transparent; }
  .pcr-output-panel-content::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.15);
    border-radius: 3px;
  }
  .pcr-output-panel-content::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.25);
  }
</style>
