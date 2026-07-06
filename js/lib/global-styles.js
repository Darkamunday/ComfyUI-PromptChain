// Global styles — CSS for DOM created by vanilla JS (not Svelte components).
// Scoped component CSS lives in each .svelte file's <style> block.

let injected = false;

export function injectStyles() {
  if (injected) return;
  injected = true;
  const style = document.createElement("style");
  style.textContent = `
    /* Override containment on node to allow tooltips/autocomplete to escape */
    .lg-node:has(.pcr-editor) {
      contain: none !important;
      overflow: visible !important;
    }
    .lg-node-widgets:has(.pcr-editor) {
      overflow: visible !important;
    }
    .pcr-editor {
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: visible !important;
    }
    /* Autocomplete dropdown: trap scroll and pointer events */
    .cm-tooltip-autocomplete {
      max-height: 300px !important;
      overflow: hidden !important;
      pointer-events: auto !important;
    }
    .cm-tooltip-autocomplete > ul {
      max-height: 300px !important;
      overflow-y: auto !important;
      overscroll-behavior: contain !important;
      pointer-events: auto !important;
    }
    .cm-tooltip-autocomplete > ul > li {
      pointer-events: auto !important;
      cursor: pointer !important;
    }

    /* lock / disable overlays — override body background only */
    [data-node-id].pcr-node-locked [data-testid^="node-body-"] {
      background:
        repeating-linear-gradient(
          45deg,
          transparent,
          transparent 12px,
          rgba(0, 0, 0, 0.08) 12px,
          rgba(0, 0, 0, 0.08) 18px
        ),
        rgba(158, 110, 25, 0.85) !important;
    }
    [data-node-id].pcr-node-disabled [data-testid^="node-body-"] {
      background:
        repeating-linear-gradient(
          -45deg,
          transparent,
          transparent 12px,
          rgba(0, 0, 0, 0.1) 12px,
          rgba(0, 0, 0, 0.1) 18px
        ),
        rgba(74, 26, 26, 0.85) !important;
    }
    /* darken widget wrapper over lock/disable stripes */
    .pcr-node-locked [node-type="PromptChain_PromptChain"],
    .pcr-node-disabled [node-type="PromptChain_PromptChain"] {
      background: #0000007a;
      border-radius: 4px;
    }
    /* 2.0 mode: resize handler measures content height at --node-height:0.
       this min-height controls the height floor (192px total with header/padding). */
    .lg-node-widgets .pcr-editor {
      min-height: 92px;
    }
    .pcr-node-content {
      display: flex;
      flex-direction: column;
      flex: 1 1 0;
      min-height: 0;
    }
    .pcr-editor-row {
      display: flex;
      flex-direction: row;
      flex: 1 1 0;
      min-height: 0;
      overflow: visible !important;
    }
    /* Vertical stack of editor + output panel, living inside .pcr-editor-row
       alongside the image panel. Lets the image panel span the full height
       of the editor row while the output panel only takes width from the
       editor column — same split as the fullscreen layout. */
    .pcr-editor-stack {
      flex: 1 1 0;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow: visible;
    }
    .pcr-editor-frame {
      flex: 1 1 0;
      min-height: 0;
      min-width: 0;
      overflow: visible;
      display: flex;
      flex-direction: column;
    }
    /* AI Assistant panel — docks to the left of the editor stack inside
       .pcr-editor-row (node mode) and inside the focused pane's main
       column wrapper (fullscreen). Width is driven by inline style and
       persisted via node.properties.pcrAiPanelWidth. The panel itself
       is the editor surface (rgba(0,0,0,0.46) overlay in node mode,
       --pcr-fs-editor-surface in fullscreen — applied in EditorGroup);
       inside, a 3-section card (header / body / composer) floats on
       that surface with continuous borders rendered by each section. */
    .pcr-ai-panel {
      flex-shrink: 0;
      min-width: 200px;
      display: flex;
      flex-direction: column;
      background: #00000073;
      color: #ddd;
      font-size: 14px;
      overflow: hidden;
      cursor: auto;
      user-select: text;
      position: relative;
    }
    /* Full-panel image drop target overlay. pointer-events:none so the
       drag/drop events pass through to the panel's own handlers. */
    .pcr-ai-drop-overlay {
      position: absolute;
      inset: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(216, 165, 54, 0.10);
      border: 2px dashed #d8a536;
      border-radius: 6px;
      pointer-events: none;
    }
    .pcr-ai-drop-overlay-msg {
      padding: 8px 16px;
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.6);
      color: #f0bd49;
      font-size: 13px;
      font-weight: 600;
    }
    /* Node-mode divider: 1px guide line so the contextual card can sit
       flush against it. Hover brightens for the resize affordance.
       Fullscreen overrides this to a wider editor-surface stripe in
       EditorGroup.svelte. */
    .pcr-ai-divider {
      width: 1px;
      background: #262626;
      cursor: col-resize;
      flex-shrink: 0;
      transition: background 0.15s;
    }
    .pcr-ai-divider:hover {
      background: rgba(255, 255, 255, 0.2);
    }
    /* Three-section card: header is the top, body the middle, composer
       the bottom. Each section paints its own portion of the card
       border so the rectangle is continuous without internal seams.
       Outer corners (top of header, bottom of composer) get the
       border-radius. */
    .pcr-ai-panel-header {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 6px 6px 10px;
      height: 36px;
      flex-shrink: 0;
    }
    .pcr-ai-panel-icon {
      color: #a855f7;
      font-size: 14px;
      flex-shrink: 0;
    }
    .pcr-ai-panel-title {
      font-weight: 600;
      color: #d1a137;
      flex-shrink: 0;
    }
    .pcr-ai-panel-spacer {
      flex: 1 1 auto;
    }
    .pcr-ai-panel-close {
      cursor: pointer;
      color: #585858;
      font-weight: 600;
      padding: 2px 6px;
      border-radius: 3px;
      flex-shrink: 0;
      font-size: 13px;
    }
    .pcr-ai-panel-close:hover {
      background: rgba(255, 255, 255, 0.1);
      color: #fff;
    }
    .pcr-ai-panel-body {
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
      padding: 6px 10px 10px 10px;
      scrollbar-width: thin;
      scrollbar-color: #333 transparent;
    }
    .pcr-ai-panel-body::-webkit-scrollbar {
      width: 8px;
    }
    .pcr-ai-panel-body::-webkit-scrollbar-track {
      background: transparent;
    }
    .pcr-ai-panel-body::-webkit-scrollbar-thumb {
      background: #2a2a2a;
      border-radius: 4px;
    }
    .pcr-ai-panel-body::-webkit-scrollbar-thumb:hover {
      background: #3a3a3a;
    }
    .pcr-ai-panel-placeholder {
      color: #888;
      line-height: 1.6;
    }
    .pcr-ai-panel-chip {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 500;
      margin: 0 2px;
    }
    .pcr-ai-panel-chip-remove {
      background: rgba(239, 68, 68, 0.18);
      color: #f87171;
    }
    .pcr-ai-panel-chip-add {
      background: rgba(34, 197, 94, 0.18);
      color: #4ade80;
    }
    .pcr-ai-panel-chip-keep {
      background: rgba(255, 255, 255, 0.08);
      color: #aaa;
    }
    /* Phase 1 result rendering — JSON preview + warnings + status. */
    .pcr-ai-panel-status {
      color: #888;
      font-style: italic;
      font-size: 13px;
    }
    .pcr-ai-panel-elapsed {
      margin-left: 8px;
      color: #666;
      font-style: normal;
      font-variant-numeric: tabular-nums;
      font-size: 12px;
    }
    .pcr-ai-panel-dots span {
      display: inline-block;
      animation: pcr-ai-dot 1.2s infinite;
      opacity: 0.3;
    }
    .pcr-ai-panel-dots span:nth-child(2) { animation-delay: 0.2s; }
    .pcr-ai-panel-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes pcr-ai-dot {
      0%, 60%, 100% { opacity: 0.3; }
      30% { opacity: 1; }
    }
    .pcr-ai-panel-error {
      color: #f87171;
      background: rgba(248, 113, 113, 0.08);
      border-radius: 4px;
      padding: 8px;
      font-size: 12px;
      line-height: 1.5;
    }
    .pcr-ai-panel-error strong { display: block; margin-bottom: 4px; color: #f87171; }
    .pcr-ai-panel-info {
      color: #d1a137;
      background: rgba(209, 161, 55, 0.08);
      border-radius: 4px;
      padding: 8px;
      font-size: 12px;
      line-height: 1.5;
    }
    .pcr-ai-panel-result { display: flex; flex-direction: column; gap: 8px; }
    .pcr-ai-panel-notes {
      font-size: 13px;
      color: #aaa;
      line-height: 1.5;
      font-style: italic;
    }
    /* Phase 2 diff sections — three lists of remove/add/keep chips with
       a count label per section. Apply / Reject buttons live below. */
    .pcr-ai-panel-diff-section {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .pcr-ai-panel-diff-label {
      font-size: 11px;
      color: #888;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .pcr-ai-panel-diff-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .pcr-ai-panel-diff-chip {
      display: inline-block;
      padding: 2px 7px;
      border-radius: 3px;
      font-size: 12px;
      font-family: ui-monospace, Consolas, monospace;
      line-height: 1.4;
      word-break: break-word;
    }
    .pcr-ai-panel-diff-chip {
      background: rgba(255, 255, 255, 0.07);
    }
    .pcr-ai-panel-diff-chip--polarity-pos {
      color: #ddd;
    }
    .pcr-ai-panel-diff-chip--polarity-neg {
      color: #fca5a5;
    }
    .pcr-ai-panel-diff-chip--action-remove {
      text-decoration: line-through;
      text-decoration-thickness: 1px;
      opacity: 0.85;
    }
    .pcr-ai-panel-diff-chip--action-remove::before {
      content: "\\2715";
      display: inline-block;
      margin-right: 4px;
      color: #ef4444;
      font-weight: 700;
      text-decoration: none;
    }
    /* Natlang prose section body — full paragraph rendered as one block,
       not chip-per-token. Differentiates added (white) vs removed
       (struck-through) prose at the section level. */
    .pcr-ai-panel-diff-prose {
      padding: 6px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-family: ui-sans-serif, system-ui, sans-serif;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(255, 255, 255, 0.04);
      color: #ddd;
    }
    .pcr-ai-panel-diff-prose--polarity-neg {
      color: #fca5a5;
    }
    .pcr-ai-panel-diff-prose--action-remove {
      text-decoration: line-through;
      text-decoration-thickness: 1px;
      opacity: 0.7;
    }
    .pcr-ai-panel-actions {
      display: flex;
      gap: 6px;
      margin-top: 4px;
    }
    .pcr-ai-panel-action {
      flex: 1;
      padding: 6px 10px;
      border-radius: 4px;
      border: 1px solid transparent;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.12s, border-color 0.12s;
    }
    .pcr-ai-panel-action--apply {
      background: #204724;
      color: #e1f5e3;
    }
    .pcr-ai-panel-action--apply:hover { background: #1e6a26; }
    .pcr-ai-panel-action--reject {
      background: transparent;
      color: #aaa;
      border-color: #333;
    }
    .pcr-ai-panel-action--reject:hover {
      background: rgba(255, 255, 255, 0.04);
      color: #ddd;
    }
    .pcr-ai-panel-warn {
      background: rgba(251, 191, 36, 0.08);
      border-left: 2px solid rgba(251, 191, 36, 0.6);
      border-radius: 3px;
      padding: 6px 8px;
      font-size: 11.5px;
      line-height: 1.5;
      color: #fbbf24;
    }
    .pcr-ai-panel-warn strong { display: block; margin-bottom: 4px; color: #fbbf24; }
    .pcr-ai-panel-link {
      align-self: flex-start;
      background: transparent;
      border: none;
      color: #888;
      cursor: pointer;
      font-size: 11.5px;
      padding: 2px 0;
      text-decoration: underline;
    }
    .pcr-ai-panel-link:hover { color: #ccc; }
    /* Composer — bottom section: textarea on top, toolbar row at the
       bottom. Hairline at the top separates it from the body above. */
    .pcr-ai-panel-composer {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      padding: 6px 4px 4px 4px;
      background: #0000001f;
      border-top: 1px solid #2b2b2b;
    }
    /* Input card — single bordered surface wrapping textarea + toolbar.
       Border/background live here; the textarea inside is borderless and
       transparent so the card reads as one unified input region. */
    .pcr-ai-panel-input-card {
      display: flex;
      flex-direction: column;
      border-radius: 6px;
    }
    /* ── Image attach (composer) ────────────────────────────────── */
    .pcr-ai-attach-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 6px 6px 0 6px;
    }
    .pcr-ai-attach-chip {
      position: relative;
      width: 48px;
      height: 48px;
      border-radius: 5px;
      overflow: hidden;
      border: 1px solid #3a3a3a;
      background: #00000033;
    }
    .pcr-ai-attach-chip img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .pcr-ai-attach-loading {
      display: flex;
      align-items: center;
      justify-content: center;
      color: #888;
      font-size: 18px;
    }
    .pcr-ai-attach-remove {
      position: absolute;
      top: 1px;
      right: 1px;
      width: 16px;
      height: 16px;
      line-height: 14px;
      text-align: center;
      border: none;
      border-radius: 3px;
      background: rgba(0, 0, 0, 0.6);
      color: #eee;
      font-size: 13px;
      cursor: pointer;
      padding: 0;
    }
    .pcr-ai-attach-remove:hover { background: rgba(180, 40, 40, 0.85); }
    .pcr-ai-attach-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      background: transparent;
      border: none;
      color: #888;
      cursor: pointer;
      padding: 2px 4px;
      border-radius: 4px;
      transition: color 0.12s ease, background 0.12s ease;
    }
    .pcr-ai-attach-btn:hover { color: #ddd; background: rgba(255, 255, 255, 0.06); }
    .pcr-ai-attach-btn:disabled { opacity: 0.4; cursor: default; }
    .pcr-ai-vision-warn {
      color: #d8a536;
      font-size: 12px;
      cursor: help;
      margin-left: -2px;
    }
    /* ── Image thumbnails in chat turns ─────────────────────────── */
    .pcr-ai-chat-turn-images {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .pcr-ai-chat-turn-thumb {
      max-width: 160px;
      max-height: 160px;
      border-radius: 6px;
      border: 1px solid #3a3a3a;
      display: block;
    }
    .pcr-ai-panel-input {
      box-sizing: border-box;
      background: transparent;
      border: none;
      color: #ddd;
      font-size: 14px;
      font-family: inherit;
      line-height: 1.5;
      margin: 0;
      padding: 6px 6px 2px 6px;
      outline: none;
      resize: none;
      overflow-y: hidden;
      max-height: 160px;
      min-height: 32px;
    }
    .pcr-ai-panel-input::placeholder { color: #666; }
    .pcr-ai-panel-composer-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 6px 6px 6px;
      gap: 6px;
    }
    .pcr-ai-panel-composer-group {
      display: flex;
      align-items: center;
      gap: 2px;
    }
    .pcr-ai-panel-tool-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 5px;
      color: #888;
      cursor: pointer;
      transition: background 0.12s, color 0.12s;
    }
    .pcr-ai-panel-tool-btn:hover {
      background: rgba(255, 255, 255, 0.06);
      color: #ddd;
    }
    .pcr-ai-panel-submit {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 26px;
      padding: 0;
      background: #204724;
      border: none;
      border-radius: 5px;
      color: #e1f5e3;
      cursor: pointer;
      transition: background 0.12s, opacity 0.12s;
    }
    .pcr-ai-panel-submit:hover:not(:disabled),
    .pcr-ai-panel-submit:active:not(:disabled) {
      background: #1e6a26;
    }
    .pcr-ai-panel-submit:disabled {
      opacity: 0.4;
      cursor: default;
    }
    .pcr-ai-panel-submit.is-stop {
      background: #5a1c1c;
      color: #fde2e2;
    }
    .pcr-ai-panel-submit.is-stop:hover,
    .pcr-ai-panel-submit.is-stop:active {
      background: #862828;
    }
    .pcr-ai-queued {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      margin: 0 0 6px 0;
      background: rgba(74, 60, 30, 0.7);
      border: 1px solid #6e5728;
      border-radius: 5px;
      font-size: 11px;
      color: #f0e0a0;
    }
    .pcr-ai-queued-label {
      font-weight: 600;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      font-size: 10px;
      opacity: 0.8;
      flex: 0 0 auto;
    }
    .pcr-ai-queued-text {
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pcr-ai-queued-cancel {
      flex: 0 0 auto;
      width: 18px;
      height: 18px;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 3px;
      color: inherit;
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
    }
    .pcr-ai-queued-cancel:hover {
      background: rgba(0, 0, 0, 0.25);
    }
    .pcr-ai-panel-clearchat {
      font-size: 14px;
      margin-right: 2px;
    }
    /* ── Chat timeline (agent loop) ─────────────────────────────── */
    .pcr-ai-chat-timeline {
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 4px 2px;
      min-height: 100%;
    }
    .pcr-ai-chat-empty-hint {
      color: #888;
      font-size: 12.5px;
      line-height: 1.55;
      padding: 4px 2px;
    }
    .pcr-ai-chat-turn {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .pcr-ai-chat-role-label {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #777;
    }
    .pcr-ai-chat-role-label--user { color: #d1a137; }
    .pcr-ai-chat-role-label--assistant { color: #a855f7; }
    .pcr-ai-chat-user-bubble {
      background: #0000002b;
      border: 1px solid rgb(66 59 4);
      border-radius: 5px;
      padding: 6px 10px;
      font-size: 14px;
      line-height: 1.5;
      color: #d8a536;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .pcr-ai-chat-assistant-prose {
      padding: 2px 0;
      font-size: 14px;
      line-height: 1.55;
      color: #ccc;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .pcr-ai-chat-assistant-prose .pcr-ai-chat-list {
      margin: 4px 0 4px 0;
      padding: 0 0 0 18px;
      list-style: disc outside;
      /* Override container's pre-wrap so newlines between <li> tags
         don't render as extra blank lines. */
      white-space: normal;
    }
    .pcr-ai-chat-assistant-prose .pcr-ai-chat-list li {
      margin: 1px 0;
      padding: 0;
    }
    .pcr-ai-chat-assistant-prose strong {
      color: #e6e6e6;
      font-weight: 600;
    }
    .pcr-ai-chat-thinking {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 2px;
      color: #888;
      font-size: 12px;
    }
    /* ── Condensed-history marker ───────────────────────────────── */
    .pcr-ai-chat-condensed {
      align-self: center;
      color: #6f6f6f;
      font-size: 11px;
      font-style: italic;
      letter-spacing: 0.02em;
      padding: 2px 4px;
      cursor: default;
    }
    /* ── Per-turn actions (copy / regenerate / edit) ────────────── */
    .pcr-ai-turn-actions {
      display: flex;
      gap: 4px;
      margin-top: 2px;
      opacity: 0;
      transition: opacity 0.12s ease;
    }
    .pcr-ai-chat-turn:hover .pcr-ai-turn-actions,
    .pcr-ai-chat-turn:focus-within .pcr-ai-turn-actions {
      opacity: 1;
    }
    .pcr-ai-turn-action {
      background: transparent;
      border: 1px solid transparent;
      border-radius: 4px;
      padding: 1px 6px;
      font-size: 11px;
      color: #888;
      cursor: pointer;
      transition: color 0.12s ease, background 0.12s ease;
    }
    .pcr-ai-turn-action:hover {
      color: #ddd;
      background: rgba(255, 255, 255, 0.06);
    }
    .pcr-ai-turn-action:disabled {
      opacity: 0.4;
      cursor: default;
    }
    .pcr-ai-turn-action--primary {
      color: #d8a536;
    }
    .pcr-ai-turn-action--primary:hover {
      color: #f0bd49;
      background: rgba(216, 165, 54, 0.12);
    }
    /* ── Inline edit (user turn) ────────────────────────────────── */
    .pcr-ai-chat-edit {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .pcr-ai-chat-edit-input {
      width: 100%;
      box-sizing: border-box;
      background: #0000002b;
      border: 1px solid rgb(96 86 8);
      border-radius: 5px;
      padding: 6px 10px;
      font-size: 14px;
      line-height: 1.5;
      color: #d8a536;
      font-family: inherit;
      resize: none;
      overflow: hidden;
    }
    .pcr-ai-chat-edit-input:focus {
      outline: none;
      border-color: #d8a536;
    }
    .pcr-ai-chat-edit-actions {
      display: flex;
      gap: 6px;
      justify-content: flex-end;
    }
    /* ── Proposal card ──────────────────────────────────────────── */
    .pcr-ai-proposal-card {
      border: 1px solid #2b2b2b;
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.18);
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 4px;
    }
    .pcr-ai-proposal-header {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 10px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }
    .pcr-ai-proposal-pill {
      padding: 2px 6px;
      border-radius: 3px;
      font-weight: 600;
      flex-shrink: 0;
    }
    .pcr-ai-proposal-pill--pending {
      background: rgba(255, 165, 0, 0.18);
      color: #f59e0b;
    }
    .pcr-ai-proposal-pill--applied {
      background: rgba(34, 197, 94, 0.18);
      color: #4ade80;
    }
    .pcr-ai-proposal-pill--rejected {
      background: rgba(255, 255, 255, 0.04);
      color: #666;
      text-decoration: line-through;
    }
    .pcr-ai-proposal-pill--failed {
      background: rgba(248, 113, 113, 0.18);
      color: #f87171;
    }
    .pcr-ai-proposal-request {
      color: #888;
      text-transform: none;
      letter-spacing: 0;
      font-weight: 400;
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1 1 auto;
      min-width: 0;
    }
    .pcr-ai-proposal-time {
      color: #555;
      margin-left: auto;
      flex-shrink: 0;
    }
    .pcr-ai-proposal-body {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .pcr-ai-proposal-empty {
      color: #666;
      font-style: italic;
      font-size: 11.5px;
    }
    .pcr-ai-proposal-error {
      color: #f87171;
      font-size: 11.5px;
      line-height: 1.45;
      padding: 4px 0;
    }
    /* ── Mode dropdown (composer toolbar) ───────────────────────── */
    .pcr-ai-mode-dropdown-btn {
      display: flex;
      align-items: center;
      gap: 4px;
      height: 26px;
      padding: 0 8px;
      background: transparent;
      border: 1px solid #333;
      border-radius: 5px;
      color: #aaa;
      font-size: 11px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.12s, border-color 0.12s, color 0.12s;
      white-space: nowrap;
    }
    .pcr-ai-mode-dropdown-btn:hover {
      background: rgba(255, 255, 255, 0.04);
      color: #ddd;
      border-color: #444;
    }
    .pcr-ai-mode-dropdown-btn-caret {
      font-size: 9px;
      opacity: 0.7;
    }
    .pcr-ai-mode-menu {
      min-width: 220px;
      padding: 4px;
    }
    .pcr-ai-mode-menu-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
    }
    .pcr-ai-mode-menu-row-text {
      display: flex;
      flex-direction: column;
      gap: 2px;
      flex: 1 1 auto;
    }
    .pcr-ai-mode-menu-row-label {
      font-size: 12px;
      color: #ddd;
      font-weight: 500;
    }
    .pcr-ai-mode-menu-row-sub {
      font-size: 10.5px;
      color: #777;
    }
    /* Commands menu (slash button popover) */
    .pcr-ai-panel-tool-btn--active {
      color: #d1a137;
    }
    .pcr-ai-commands-menu {
      min-width: 240px;
      padding: 6px 4px;
    }
    .pcr-ai-commands-section-label {
      padding: 2px 8px 6px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #777;
    }
    .pcr-ai-commands-toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 8px;
    }
    .pcr-ai-commands-toggle-text {
      display: flex;
      flex-direction: column;
      gap: 2px;
      flex: 1 1 auto;
    }
    .pcr-ai-commands-toggle-label {
      font-size: 12px;
      color: #ddd;
      font-weight: 500;
    }
    .pcr-ai-commands-toggle-sub {
      font-size: 10.5px;
      color: #777;
    }
    .pcr-ai-commands-toggle-state {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 2px 6px;
      border-radius: 3px;
      background: rgba(255, 255, 255, 0.04);
      color: #666;
      flex-shrink: 0;
    }
    .pcr-ai-commands-toggle-state.pcr-on {
      background: rgba(209, 161, 55, 0.18);
      color: #d1a137;
    }
    /* Applies to any editor mount container (node widget + fullscreen). */
    .pcr-editor-drop-active {
      position: relative;
    }
    .pcr-editor-drop-active::after {
      content: "";
      position: absolute;
      inset: 0;
      border: 2px dashed #4fc3f7;
      background: rgba(79, 195, 247, 0.08);
      pointer-events: none;
      z-index: 10;
    }
    .pcr-editor-mount {
      flex: 1 1 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow: visible;
    }
    .pcr-editor .cm-editor {
      flex: 1 1 0;
      min-height: 0;
      overflow: visible !important;
      border-radius: 0 !important;
      background-color: rgba(0, 0, 0, 0.55) !important;
    }
    .pcr-editor .cm-editor.cm-focused { outline: none; }
/* darken widget area when node has custom color */
    .pcr-editor.pcr-colored {
      background: #00000040;
    }
    /* 2.0 mode overrides */
    .lg-node-widgets .pcr-editor .cm-editor {
      background-color: rgb(0 0 0 / 46%) !important;
    }
    /* 2.0 mode: editor CM override (menubar/footer/output-panel overrides in component <style> blocks) */
    .pcr-editor .cm-scroller {
      overflow: auto;
      font-family: Consolas, Menlo, "Liberation Mono", monospace;
      font-size: var(--pcr-font-size, 13px);
      scrollbar-width: thin;
      scrollbar-color: rgba(255, 255, 255, 0.15) transparent;
    }
    .pcr-editor .cm-scroller::-webkit-scrollbar { width: 6px; height: 6px; }
    .pcr-editor .cm-scroller::-webkit-scrollbar-track { background: transparent; }
    .pcr-editor .cm-scroller::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.15);
      border-radius: 3px;
    }
    .pcr-editor .cm-scroller::-webkit-scrollbar-thumb:hover {
      background: rgba(255, 255, 255, 0.25);
    }
    .pcr-editor .cm-scroller::-webkit-scrollbar-corner { background: transparent; }
    .pcr-editor .cm-content {
      padding: 6px 0;
    }
    .pcr-editor .cm-line {
      border-left: 5px solid transparent;
      padding: 0 8px 0 4px;
    }

    /* active label highlight (switch mode) */
    .pcr-editor .cm-line.pcr-line-active {
      border-left-color: #2bc036ad;
      /* background: #ffffff05; */
      padding-left: 4px;
    }
    .pcr-editor .cm-line.pcr-line-iterate {
      border-left-color: #33bdffad;
    }
    .pcr-iterate-active .cm-line {
      opacity: 0.4;
      transition: opacity 0.15s ease;
    }
    .pcr-iterate-active .cm-line.pcr-line-active {
      opacity: 1;
    }

    /* $region markers ($mannequin1 { ... }) — regional conditioning blocks */
    .pcr-editor .pcr-region-name {
      color: #ff9d5c;
      font-weight: 700;
    }
    .pcr-editor .pcr-region-brace {
      color: #ff9d5c;
      font-weight: 700;
      opacity: 0.65;
    }

    /* wildcard conflict: dim all text when children override inline-wildcards */
    .pcr-wildcard-conflict .cm-line {
      opacity: 0.35;
    }
    .pcr-wildcard-conflict .cm-activeLine {
      opacity: 0.35;
    }

    /* Chip recognizer — invisible by default; hover reveals a pill-
       shaped status-tinted background on the chunk under the cursor.
       Editor stays calm during writing; user actively explores
       recognized chips by reaching toward them. cursor:help is the
       only persistent affordance so the user knows there's a tooltip
       to be had. */
    .pcr-chip-recognized {
      border-radius: 3px;
      padding: 1px 4px;
      margin: 0 -4px;
      transition: background 0.08s ease-out, box-shadow 0.08s ease-out;
    }
    .pcr-chip-recognized:hover {
      box-shadow: 0 0 0 1px currentColor inset;
    }
    .pcr-chip-recognized-unprocessed:hover {
      background: rgba(107, 114, 128, 0.18);
      color: #c4c4c4;
    }
    .pcr-chip-recognized-ready:hover {
      background: rgba(34, 197, 94, 0.18);
      color: #86efac;
    }
    .pcr-chip-recognized-broken:hover {
      background: rgba(220, 38, 38, 0.18);
      color: #fca5a5;
    }

    /* Hover tooltip surfacing chip metadata. Pointer-events:none so
       hover doesn't get stuck on the tooltip itself. */
    .pcr-chip-tooltip {
      position: fixed;
      z-index: 100020;
      pointer-events: none;
      background: #1f1830;
      border: 1px solid #4a3a5a;
      border-radius: 6px;
      padding: 0 0 6px;
      width: 340px;
      box-shadow: 0 6px 20px rgba(0, 0, 0, 0.55);
      font-size: 12px;
      color: #d4b8ff;
      overflow: hidden;
    }
    .pcr-chip-tooltip-thumb {
      display: block;
      width: 100%;
      height: 180px;
      object-fit: contain;
      object-position: center top;
      background: #151021;
    }
    .pcr-chip-tooltip-title {
      padding: 8px 12px 4px;
      font-size: 13px;
      font-weight: 600;
      color: #e6def5;
      background: #261d3a;
      border-bottom: 1px solid #3a2a4a;
    }
    .pcr-chip-tooltip-sub {
      padding: 4px 12px 6px;
      font-size: 10px;
      color: #8a7aa3;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      background: #261d3a;
      border-bottom: 1px solid #3a2a4a;
    }
    .pcr-chip-tooltip-row {
      padding: 6px 12px 0;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .pcr-chip-tooltip-label {
      color: #8a7aa3;
      font-size: 9px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    .pcr-chip-tooltip-value {
      color: #e6def5;
      line-height: 1.4;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .pcr-chip-tooltip-mono {
      font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
      font-size: 11px;
      color: #c0a8e0;
    }
    .pcr-chip-tooltip-status {
      margin: 8px 12px 0;
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 3px 8px;
      border-radius: 3px;
      display: inline-block;
    }
    .pcr-chip-tooltip-status.pcr-chip-status-normalized { background: #1a3a1f; color: #4ade80; }
    .pcr-chip-tooltip-status.pcr-chip-status-broken     { background: #3a1a1a; color: #f87171; }
    .pcr-chip-tooltip-status.pcr-chip-status-unprocessed { background: #2a2a2a; color: #a0a0a0; }

    /* character chip tooltip — chip-ref migration sections */
    .pcr-chip-tooltip-section-header {
      margin: 10px 12px 4px;
      font-size: 9px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #6b7280;
    }
    .pcr-chip-tooltip-identity-token {
      margin: 0 12px 2px;
      font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
      font-size: 11px;
      color: #c0a8e0;
    }
    .pcr-chip-tooltip-identity-extras {
      margin: 0 12px 4px;
      font-family: ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace;
      font-size: 11px;
      color: #9ca3af;
    }
    .pcr-chip-tooltip-pill-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin: 2px 12px 0;
    }
    .pcr-chip-tooltip-pill {
      font-size: 10px;
      line-height: 1.2;
      padding: 3px 7px;
      border-radius: 10px;
      border: 1px solid transparent;
      white-space: nowrap;
    }
    .pcr-chip-tooltip-pill-normalized {
      background: rgba(34, 197, 94, 0.14);
      border-color: rgba(34, 197, 94, 0.35);
      color: #86efac;
    }
    .pcr-chip-tooltip-pill-broken {
      background: rgba(220, 38, 38, 0.14);
      border-color: rgba(220, 38, 38, 0.35);
      color: #fca5a5;
    }
    .pcr-chip-tooltip-pill-unprocessed {
      background: rgba(107, 114, 128, 0.14);
      border-color: rgba(107, 114, 128, 0.35);
      color: #c4c4c4;
    }
    .pcr-chip-tooltip-enhancer {
      margin: 2px 12px 0;
      font-style: italic;
      font-size: 12px;
      color: #d8b4fe;
      line-height: 1.35;
    }

    /* slot dropdowns */
    .pcr-slot-dropdown {
      cursor: pointer !important;
      transition: color 0.15s;
      border: 1px solid transparent;
      background: transparent;
      border-radius: 4px;
      padding: 0px 4px;
    }
    .pcr-slot-dropdown:first-child {
      padding-left: 2px;
    }
    .pcr-slot-arrow {
      margin-left: 2px;
      margin-right: 1px;
      font-size: 13px;
      opacity: 0.5;
      transition: opacity 0.15s;
    }
    .pcr-slot-dropdown:hover .pcr-slot-arrow {
      opacity: 0.7;
    }
    .pcr-slot-dropdown:hover {
      border-color: #ffffff25;
      background: #ffffff10;
    }
    .pcr-slot-dropdown-combine {
      color: #e79454 !important;
    }
    .pcr-slot-dropdown-combine:hover {
      color: rgb(255, 185, 79) !important;
    }
    .pcr-slot-dropdown-active {
      color: #73d952 !important;
    }
    .pcr-slot-dropdown-active:hover {
      color: #9aff6b !important;
    }
    .pcr-slot-dropdown-none {
      color: #b0b0b0 !important;
    }
    .pcr-slot-dropdown-none:hover {
      color: #d0d0d0 !important;
    }
    .pcr-slot-dropdown-iterate {
      color: #33bdff !important;
    }
    .pcr-slot-dropdown-iterate:hover {
      color: #66d4ff !important;
    }
    .pcr-slot-dropdown-roll {
      color: #da3e65 !important;
    }
    .pcr-slot-dropdown-roll:hover {
      color: #ff4573 !important;
    }
    .pcr-slot-dropdown-open {
      color: #b4ff5b !important;
      border: 1px solid #ffffff52;
      background: #04040440;
    }

    /* dimmed slot (non-selected in switch mode) */
    .pcr-slot-dimmed {
      opacity: 0.35 !important;
    }
    .pcr-slot-dimmed .pcr-slot-dropdown:hover {
      opacity: 1;
    }

    /* ── Mode menu shared dropdown (portaled to body, 5+ consumers) ── */
    .pcr-mode-menu {
      position: fixed;
      min-width: 240px;
      max-width: 320px;
      background: rgba(30, 30, 30, 0.98);
      backdrop-filter: blur(12px);
      border: 1px solid #444;
      border-radius: 6px;
      z-index: 100000;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      font-family: system-ui, -apple-system, sans-serif;
      overflow: hidden;
    }
    .pcr-mode-menu-modes {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 4px;
      background: #252525;
      border-bottom: 1px solid #444;
    }
    .pcr-mode-menu-mode-option {
      padding: 4px 4px !important;
      font-size: 12px !important;
      border-radius: 4px;
    }
    .pcr-mode-menu-mode-option:hover:not(.pcr-mode-menu-disabled) {
      background: #3a3a3a;
    }
    .pcr-mode-menu-search-container {
      padding: 4px;
    }
    .pcr-mode-menu-search {
      width: 100%;
      padding: 6px 10px;
      background: #333;
      border: 1px solid #444;
      border-radius: 4px;
      color: #ddd;
      font-size: 12px;
      outline: none;
      transition: border-color 0.15s;
      box-sizing: border-box;
    }
    .pcr-mode-menu-search:focus { border-color: #666; }
    .pcr-mode-menu-search::placeholder { color: #666; }
    .pcr-mode-menu-separator {
      height: 1px;
      background: #444;
    }
    .pcr-mode-menu-list {
      max-height: 300px;
      overflow-y: auto;
      overflow-x: hidden;
    }
    .pcr-mode-menu-item {
      display: flex;
      align-items: center;
      padding: 4px 8px;
      font-size: 12px;
      color: #ddd;
      cursor: pointer;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: background 0.1s;
    }
    .pcr-mode-menu-item:hover { background: #3a3a3a; }
    .pcr-mode-menu-item--danger { color: #e74c3c; }
    .pcr-mode-menu-item--danger:hover { background: #8b2020; color: #fff; }
    .pcr-mode-menu-disabled {
      color: #666;
      cursor: not-allowed;
    }
    .pcr-mode-menu-disabled:hover { background: transparent; }
    .pcr-mode-menu-selected {
      color: rgba(94, 211, 87, 0.95);
    }
    .pcr-mode-menu-keyboard-selected {
      background: rgba(79, 195, 247, 0.2);
      outline: 1px solid rgba(79, 195, 247, 0.4);
      outline-offset: -1px;
    }
    .pcr-mode-menu-item > span:first-child {
      flex: 1;
    }
    .pcr-mode-menu-check {
      margin-left: 8px;
      color: rgba(94, 211, 87, 0.95);
      font-size: 11px;
      flex-shrink: 0;
    }
    .pcr-mode-menu-reset {
      margin-left: 4px;
      width: 20px;
      text-align: center;
      color: #33bdff;
      font-size: 14px;
      cursor: pointer;
      flex-shrink: 0;
      opacity: 0.7;
      border: 1px solid #8e8e8e69;
      background: #00000042;
      border-radius: 3px;
      line-height: 18px;
      padding-bottom: 1px;
    }
    .pcr-mode-menu-reset:hover {
      opacity: 1;
    }
    .pcr-mode-menu-label {
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1;
      min-width: 0;
    }
    .pcr-mode-menu-highlight {
      background: rgba(255, 213, 79, 0.3);
      color: #ffd54f;
      border-radius: 2px;
      padding: 0 1px;
    }
    .pcr-mode-menu-empty {
      padding: 16px 12px;
      font-size: 12px;
      color: #888;
      font-style: italic;
      text-align: center;
    }

    /* editor context menu (right-click) */
    .pcr-context-menu {
      min-width: 160px;
      max-width: 220px;
      max-height: 65vh;
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior: contain;
      padding: 4px 0;
    }
    .pcr-context-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 12px;
      font-size: 12px;
      color: #ddd;
      cursor: pointer;
      user-select: none;
      transition: background 0.1s;
    }
    .pcr-context-item:hover:not(.pcr-context-disabled) {
      background: #3a3a3a;
    }
    .pcr-context-disabled {
      color: #555;
      cursor: default;
    }
    .pcr-context-disabled .pcr-context-shortcut {
      color: #444;
    }
    .pcr-context-label {
      flex: 1;
    }
    .pcr-context-shortcut {
      font-size: 11px;
      color: #666;
      margin-left: 16px;
    }
    .pcr-context-separator {
      height: 1px;
      background: #444;
      margin: 4px 0;
    }
    .pcr-context-submenu {
      position: fixed;
      min-width: 140px;
      max-width: 300px;
    }
    .pcr-context-submenu .pcr-context-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pcr-context-preview {
      position: fixed;
      width: 128px;
      background: #1a1a1a;
      border: 1px solid #444;
      border-radius: 4px;
      padding: 3px;
      z-index: 10001;
      pointer-events: none;
    }
    .pcr-context-preview img {
      width: 100%;
      height: auto;
      border-radius: 2px;
      display: block;
    }
    .pcr-context-kw-tooltip {
      position: fixed;
      max-width: 400px;
      padding: 6px 10px;
      background: #1a1a1a;
      border: 1px solid #555;
      border-radius: 4px;
      font-size: 11px;
      color: #ccc;
      line-height: 1.4;
      z-index: 10001;
      pointer-events: none;
      white-space: pre-wrap;
      word-break: break-word;
    }

    /* ── Model indicator (vanilla JS, model-indicator-bridge.js) ── */
    .pcr-model-indicator {
      display: flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      height: 100%;
      padding: 0 4px 0 8px;
      transition: color 0.15s;
      min-width: 0;
      overflow: hidden;
    }
    .pcr-model-disconnected .pcr-model-indicator-label {
      color: rgb(247, 180, 79);
    }
    .pcr-model-disconnected .pcr-model-indicator-arrow {
      color: rgb(247, 180, 79);
    }
    .pcr-model-disconnected:hover .pcr-model-indicator-label {
      color: rgb(255, 200, 110);
    }
    .pcr-model-disconnected:hover .pcr-model-indicator-arrow {
      color: rgb(255, 200, 110);
    }
    .pcr-model-connected .pcr-model-indicator-label {
      color: rgba(255, 255, 255, 0.5);
    }
    .pcr-model-connected .pcr-model-indicator-arrow {
      color: rgba(255, 255, 255, 0.4);
    }
    .pcr-model-connected:hover .pcr-model-indicator-label,
    .pcr-model-indicator-open .pcr-model-indicator-label {
      color: rgb(130, 230, 120);
    }
    .pcr-model-connected:hover .pcr-model-indicator-arrow,
    .pcr-model-indicator-open .pcr-model-indicator-arrow {
      color: rgb(130, 230, 120);
    }
    .pcr-model-indicator-label {
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .pcr-model-indicator-arrow {
      font-size: 10px;
    }
    .pcr-model-popup {
      position: fixed;
      background: rgba(30, 30, 30, 0.98);
      backdrop-filter: blur(12px);
      border: 1px solid #444;
      border-radius: 6px;
      box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
      padding: 8px 12px;
      z-index: 100000;
      font-size: 12px;
      color: #ddd;
    }
    .pcr-model-popup-hash {
      font-family: monospace;
      font-size: 13px;
      color: #4fc3f7;
      cursor: pointer;
      padding: 2px 0;
    }
    .pcr-model-popup-hash:hover {
      color: #80d8ff;
    }
    .pcr-model-popup-arch {
      color: rgba(255, 255, 255, 0.5);
      font-size: 11px;
      margin-top: 2px;
    }

    /* ── Tags dropdown (vanilla JS, tags-dropdown.js) ── */
    .pcr-tags-dropdown {
      position: relative;
      display: flex;
      align-items: center;
      height: 100%;
    }
    .pcr-tags-dropdown-label {
      cursor: pointer;
      color: rgba(255, 255, 255, 0.5);
      font-size: 12px;
      padding: 0 6px;
      height: 100%;
      display: flex;
      align-items: center;
      transition: color 0.15s;
      white-space: nowrap;
    }
    .pcr-tags-dropdown-label:hover { color: rgba(255, 255, 255, 0.8); }
    .pcr-tags-active { color: rgba(255, 255, 255, 0.5) !important; }
    .pcr-tags-active:hover, .pcr-tags-open { color: #80d8ff !important; }
    .pcr-tags-menu {
      position: fixed;
      bottom: auto;
      background: rgba(30, 30, 30, 0.98);
      backdrop-filter: blur(12px);
      border: 1px solid #444;
      border-radius: 6px;
      box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.5);
      min-width: 200px;
      max-width: 280px;
      z-index: 100000;
      padding: 6px 0;
      font-size: 12px;
      color: #ccc;
    }
    .pcr-tags-menu-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 4px 12px 6px;
      border-bottom: 1px solid #333;
      margin-bottom: 4px;
      font-weight: 600;
      color: #ddd;
    }
    .pcr-tags-fmt-toggle {
      font-weight: normal;
      font-size: 11px;
      color: #999;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .pcr-tags-fmt-toggle input { margin: 0; cursor: pointer; }
    .pcr-tags-mode-toggle {
      position: relative;
      display: flex;
      margin: 6px 12px 8px;
      background: rgb(0 0 0 / 22%);
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      padding: 3px;
      overflow: hidden;
    }
    .pcr-tags-mode-slider {
      position: absolute;
      top: 3px;
      bottom: 3px;
      left: 3px;
      width: calc(50% - 3px);
      background: rgb(27 133 181 / 23%);
      border: 1px solid #7ed7ffb3;
      border-radius: 4px;
      transition: transform 0.22s cubic-bezier(0.4, 0.0, 0.2, 1),
                  background 0.22s ease;
      pointer-events: none;
      box-shadow: 0 0 12px rgba(128, 216, 255, 0.18);
    }
    .pcr-tags-mode-toggle.is-natural .pcr-tags-mode-slider {
      transform: translateX(100%);
    }
    .pcr-tags-mode-btn {
      position: relative;
      z-index: 1;
      flex: 1;
      background: transparent;
      border: none;
      border-radius: 4px;
      color: #888;
      font-size: 11px;
      font-weight: 500;
      padding: 5px 6px 6px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1px;
      transition: color 0.18s ease;
    }
    .pcr-tags-mode-btn:hover:not(.active) { color: #bbb; }
    .pcr-tags-mode-btn.active { color: #80d8ff; }
    .pcr-tags-mode-btn-row {
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .pcr-tags-mode-icon {
      display: inline-flex;
      align-items: center;
      opacity: 0.75;
    }
    .pcr-tags-mode-btn.active .pcr-tags-mode-icon { opacity: 1; }
    .pcr-tags-mode-label {
      font-weight: 600;
      letter-spacing: 0.2px;
    }
    .pcr-tags-mode-icon + .pcr-tags-mode-label { margin-left: 4px; }
    .pcr-tags-mode-sample {
      font-size: 10px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      color: rgba(255, 255, 255, 0.35);
      letter-spacing: 0;
      margin-top: 1px;
    }
    .pcr-tags-mode-btn.active .pcr-tags-mode-sample { color: rgba(128, 216, 255, 0.55); }
    .pcr-tags-source-list { padding: 2px 0; }
    .pcr-tags-source-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      cursor: default;
      transition: background 0.1s;
    }
    .pcr-tags-source-row:hover { background: rgba(255, 255, 255, 0.05); }
    .pcr-tags-source-row.enabled { color: #ddd; }
    .pcr-tags-source-row:not(.enabled) { color: #777; }
    .pcr-tags-source-row.dragging { opacity: 0.4; }
    .pcr-tags-source-row.drag-over-top { border-top: 2px solid #4fc3f7; }
    .pcr-tags-source-row.drag-over-bottom { border-bottom: 2px solid #4fc3f7; }
    .pcr-tags-source-row input[type="checkbox"] { margin: 0; cursor: pointer; }
    .pcr-tags-drag-handle { color: #555; cursor: grab; font-size: 14px; }
    .pcr-tags-source-name { flex: 1; }
    .pcr-tags-source-count { color: #666; font-size: 11px; }
    .pcr-tags-priority-badge {
      background: #0e639c;
      color: #fff;
      font-size: 10px;
      padding: 1px 5px;
      border-radius: 3px;
      font-weight: 600;
    }
    .pcr-tags-menu-info {
      padding: 6px 12px 2px;
      color: #555;
      font-size: 11px;
      border-top: 1px solid #333;
      margin-top: 4px;
    }

    .pcr-tags-btn-row {
      display: flex;
      gap: 6px;
      padding: 8px 12px 6px;
      border-top: 1px solid #333;
      margin-top: 4px;
    }
    .pcr-tags-btn {
      flex: 1;
      padding: 4px 8px;
      border: none;
      border-radius: 4px;
      font-size: 11px;
      cursor: pointer;
      transition: background 0.15s;
    }
    .pcr-tags-btn:disabled { opacity: 0.35; cursor: default; }
    .pcr-tags-btn-save {
      background: #0e639c;
      color: #fff;
    }
    .pcr-tags-btn-save:hover:not(:disabled) { background: #1177bb; }
    .pcr-tags-btn-restore {
      background: #414141;
      color: #ddd;
    }
    .pcr-tags-btn-restore:hover:not(:disabled) { background: #555; }

    /* autocomplete category icons */
    .cm-completionIcon-general { background: #4fc3f7 !important; }
    .cm-completionIcon-character { background: #2ecc71 !important; }
    .cm-completionIcon-copyright { background: #9b59b6 !important; }
    .cm-completionIcon-artist { background: #e74c3c !important; }
    .cm-completionIcon-meta { background: #95a5a6 !important; }
    .cm-completionIcon-species { background: #f39c12 !important; }
    .cm-completionIcon-lore { background: #1abc9c !important; }
    .cm-completionIcon-composition { background: #3498db !important; }

    /* ── Shared keyframe (model indicator detecting state) ── */
    @keyframes pcr-pulse {
      0%, 100% { opacity: 0.7; }
      50% { opacity: 1; }
    }

    .pcr-model-detecting .pcr-model-indicator-label {
      opacity: 0.6;
      animation: pcr-pulse 1.5s ease-in-out infinite;
    }

    /* ── Onboarding splash (vanilla JS, onboarding.js) ── */
    .pcr-onboarding-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 999999;
    }
    .pcr-onboarding-card {
      background: #1e1e1e;
      border: 1px solid #444;
      border-radius: 10px;
      padding: 32px;
      max-width: 420px;
      width: 90%;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.6);
      color: #ddd;
    }
    /* ── Welcome screen (hero splash) ── */
    .pcr-onboarding-card.pcr-welcome-mode {
      padding: 0;
      max-width: 870px;
      overflow: hidden;
      border-color: #373737;
    }
    .pcr-welcome {
      position: relative;
      width: 100%;
      aspect-ratio: 1440 / 1204;
      background-color: #08060d;
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;
      color: #fff;
    }
    .pcr-welcome::after {
      content: "";
      position: absolute; inset: 0; pointer-events: none;
      background:
        linear-gradient(to bottom, rgba(8,6,13,0.5) 0%, rgba(8,6,13,0) 16%),
        linear-gradient(to top, rgba(6,5,10,0.9) 0%, rgba(6,5,10,0) 24%);
    }
    .pcr-welcome-top {
      position: absolute; top: 24px; left: 30px; z-index: 2;
      display: flex; flex-direction: column; gap: 7px;
    }
    .pcr-welcome-logo { height: 50px; width: 230px; display: block; margin-left: -10px; }
    .pcr-welcome-steps { display: flex; align-items: center; gap: 22px; }
    .pcr-welcome-step {
      display: flex; align-items: center; gap: 8px;
      font-size: 15px; color: rgba(255,255,255,0.42);
    }
    .pcr-welcome-step .pcr-welcome-dot {
      width: 9px; height: 9px; border-radius: 50%;
      background: rgba(255,255,255,0.3);
    }
    .pcr-welcome-step.is-active { color: #fff; font-weight: 600; }
    .pcr-welcome-step.is-clickable { cursor: pointer; }
    .pcr-welcome-step.is-clickable:hover { color: rgba(255,255,255,0.78); }
    .pcr-welcome-step.is-active .pcr-welcome-dot {
      background: #f5821f; box-shadow: 0 0 8px rgba(245,130,31,0.85);
    }
    .pcr-welcome-tagline {
      position: absolute; left: 34px; bottom: 92px; z-index: 2;
      font-size: 26px; font-weight: 300; max-width: 60%;
      color: rgba(255,255,255,0.94);
      text-shadow: 0 2px 14px rgba(0,0,0,0.75);
    }
    .pcr-welcome-bar {
      position: absolute; left: 0; right: 0; bottom: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; padding: 13px 22px;
      background: #31313154;
      border-top: 1px solid #ffffff1a;
    }
    .pcr-welcome-foot { font-size: 13px; color: #ebbb21; font-weight: bold; }
    .pcr-welcome-foot a { color: inherit; text-decoration: underline; }
    .pcr-welcome-cta {
      flex: none; font-size: 16px; font-weight: 600; color: #fff;
      background: #c36216; border: none; border-radius: 8px;
      padding: 13px 30px; cursor: pointer;
      transition: background 0.15s, transform 0.05s;
    }
    .pcr-welcome-cta:hover { background: #ff912c; }
    .pcr-welcome-cta:active { transform: translateY(1px); }
    .pcr-welcome-skip {
      background: none; border: none; color: rgba(255,255,255,0.38);
      font-size: 14px; cursor: pointer; padding: 8px 4px;
      transition: color 0.15s;
    }
    .pcr-welcome-skip:hover { color: rgba(255,255,255,0.7); }
    /* Bottom action bar shared by the form steps (Setup, Extras). */
    .pcr-onboarding-bar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; margin: 22px -32px -32px; padding: 16px 22px;
      background: #31313154;
      border-top: 1px solid #ffffff1a;
      border-radius: 0 0 10px 10px;
    }
    .pcr-onboarding-logo {
      display: block;
      height: 32px;
      margin-top: -10px;
      margin-bottom: 10px;
      margin-left: -5px;
    }
    /* ── About modal (welcome hero re-opened, dismissible) ── */
    .pcr-about-card.pcr-welcome-mode { max-width: 620px; }
    .pcr-about-hero .pcr-welcome-logo { margin-left: -15px; margin-top: -5px; }
    .pcr-about-close {
      position: absolute; top: 12px; right: 16px; z-index: 3;
      background: none; border: none; color: rgba(255,255,255,0.7);
      font-size: 18px; line-height: 1; cursor: pointer; padding: 4px;
      transition: color 0.15s;
    }
    .pcr-about-close:hover { color: #fff; }
    .pcr-about-barleft { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
    .pcr-about-version {
      font-size: 12px; color: rgba(255,255,255,0.66);
      font-family: ui-monospace, Consolas, monospace;
    }
    .pcr-about-update {
      flex: none; font-size: 14px; font-weight: 500; color: #fff;
      background: rgba(20,20,26,0.55); border: 1px solid rgba(255,255,255,0.28);
      border-radius: 8px; padding: 10px 20px; cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    .pcr-about-update:hover { background: rgba(40,40,50,0.7); border-color: rgba(255,255,255,0.5); }
    .pcr-about-update:disabled { opacity: 0.55; cursor: default; }
    .pcr-onboarding-title {
      margin: 0 0 4px;
      font-size: 20px;
      font-weight: 600;
      color: #fff;
    }
    .pcr-onboarding-subtitle {
      margin: 0 0 20px;
      color: #888;
      font-size: 13px;
    }
    .pcr-onboarding-section {
      margin-bottom: 16px;
      padding: 12px;
      background: rgba(255, 255, 255, 0.04);
      border-radius: 6px;
    }
    .pcr-onboarding-toggle {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      cursor: pointer;
    }
    .pcr-onboarding-toggle input[type="checkbox"] {
      margin-top: 3px;
      cursor: pointer;
    }
    .pcr-onboarding-toggle-text {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .pcr-onboarding-toggle-title {
      font-size: 14px;
      font-weight: 500;
      color: #ddd;
    }
    .pcr-onboarding-toggle-desc {
      font-size: 12px;
      color: #888;
      line-height: 1.4;
    }
    .pcr-onboarding-info {
      font-size: 13px;
      color: #aaa;
      line-height: 1.4;
    }
    .pcr-onboarding-info strong {
      color: #ddd;
    }
    .pcr-onboarding-apikey-input {
      flex: 1;
      padding: 6px 10px;
      background: #2a2a2a;
      border: 1px solid #555;
      border-radius: 4px;
      color: #ddd;
      font-size: 13px;
      font-family: monospace;
      outline: none;
      transition: border-color 0.15s;
    }
    .pcr-onboarding-apikey-input:focus {
      border-color: #0e639c;
    }
    .pcr-onboarding-apikey-status {
      font-size: 12px;
      white-space: nowrap;
      min-width: 20px;
      color: #888;
    }
    .pcr-apikey-valid { color: #4ec96b; }
    .pcr-apikey-invalid { color: #e55; }

    .pcr-onboarding-btn {
      display: block;
      width: 100%;
      margin-top: 20px;
      padding: 10px;
      background: #0e639c;
      color: #fff;
      border: none;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s;
    }
    .pcr-onboarding-btn:hover { background: #1177bb; }
    .pcr-onboarding-btn:disabled {
      background: #444;
      cursor: default;
    }
    .pcr-onboarding-footer {
      margin: 12px 0 0;
      font-size: 12px;
      color: rgba(255, 255, 255, 0.45);
      text-align: center;
    }
    .pcr-onboarding-footer a {
      color: rgba(255, 255, 255, 0.6);
      text-decoration: none;
    }
    .pcr-onboarding-footer a:hover {
      color: #fff;
      text-decoration: underline;
    }

    /* ── Document dropdown (vanilla JS, documents.js) ── */
    .pcr-doc-dropdown {
      display: flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      height: 100%;
      padding: 0 4px 0 6px;
      color: rgba(255, 255, 255, 0.5);
      transition: color 0.15s;
      min-width: 0;
    }
    .pcr-doc-dropdown:hover { color: rgba(255, 255, 255, 0.8); }
    .pcr-doc-label {
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: rgba(255, 255, 255, 0.7);
    }
    .pcr-doc-arrow { font-size: 10px; color: rgba(255, 255, 255, 0.4); }
    .pcr-doc-menu {
      display: none;
      background: #1e1e1e;
      border: 1px solid #444;
      border-radius: 6px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      min-width: 180px;
      max-width: 280px;
      z-index: 100001;
      font-size: 13px;
      color: #ccc;
    }
    .pcr-doc-list {
      max-height: 200px;
      overflow-y: auto;
    }
    .pcr-doc-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 10px;
      cursor: pointer;
      user-select: none;
    }
    .pcr-doc-item:hover { background: #2a2a2a; }
    .pcr-doc-item-active { background: #2a2a2a; }
    .pcr-doc-item-new { border-top: 1px solid #333; }
    .pcr-doc-item-name {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .pcr-doc-item-right {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-left: 8px;
    }
    .pcr-doc-item-time { color: #666; font-size: 11px; }
    .pcr-doc-item-btn {
      background: none;
      border: none;
      color: #888;
      cursor: pointer;
      font-size: 13px;
      padding: 0 2px;
    }
    .pcr-doc-item-btn:hover { color: #fff; }
    .pcr-doc-item-delete:hover { color: #e74c3c; }
    .pcr-doc-rename-input {
      background: #111;
      border: 1px solid #4fc3f7;
      border-radius: 3px;
      color: #fff;
      font-size: 13px;
      padding: 2px 4px;
      width: 100%;
      outline: none;
    }

    /* ── Wildcard inline badges (vanilla JS, wildcard-badge.js) ── */
    .pcr-wc-badge {
      display: inline-block;
      font-size: 10px;
      line-height: 1.4;
      padding: 0 4px;
      margin-left: 2px;
      border-radius: 3px;
      cursor: pointer;
      vertical-align: middle;
      user-select: none;
      background: #24242499;
      color: #ccc;
      border: 1px solid rgb(118 118 118 / 36%);
      transition: background 0.1s;
    }
    .pcr-wc-badge:hover {
      background: #2e2e2e;
    }
    .pcr-wc-badge--switch { color: #73d952; }
    .pcr-wc-badge--combine { color: #e99e2d; }
    .pcr-wc-badge--iterate { color: #33bdff; }
    .pcr-wc-badge--none { color: #b0b0b0; opacity: 0.6; }
    .pcr-wc-badge--randomize { color: #da3e65; }

    /* ── Fullscreen delete confirm modal (vanilla JS) ── */
    .pcr-fs-overlay .pcr-modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.5);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10000;
    }
    .pcr-fs-overlay .pcr-modal {
      background: #262626;
      border: 1px solid #3a3a3a;
      border-radius: 8px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
      min-width: 320px;
      max-width: 420px;
    }
    .pcr-fs-overlay .pcr-modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px 8px;
    }
    .pcr-fs-overlay .pcr-modal-title { font-weight: 600; font-size: 14px; color: #fff; }
    .pcr-fs-overlay .pcr-modal-close {
      background: none; border: none; color: #888; cursor: pointer; padding: 4px;
    }
    .pcr-fs-overlay .pcr-modal-close:hover { color: #fff; }
    .pcr-fs-overlay .pcr-modal-body { padding: 4px 16px 12px; }
    .pcr-fs-overlay .pcr-cf-msg { font-size: 13px; color: #ccc; line-height: 1.4; margin: 0; }
    .pcr-fs-overlay .pcr-modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      padding: 8px 16px 12px;
    }
    .pcr-fs-overlay .pcr-modal-btn {
      padding: 6px 16px;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      font-size: 13px;
    }
    .pcr-fs-overlay .pcr-modal-btn-secondary { background: #3a3a3a; color: #ccc; }
    .pcr-fs-overlay .pcr-modal-btn-secondary:hover { background: #4a4a4a; }
    .pcr-fs-overlay .pcr-modal-btn-danger { background: #8b2020; color: #fff; }
    .pcr-fs-overlay .pcr-modal-btn-danger:hover { background: #b42a2a; }

    /* PrimeVue toasts default to a z-index below the fullscreen overlay
       (which is 9999). Lift them above while the overlay is mounted so
       save/status toasts remain visible. */
    body:has(.pcr-fs-overlay) .p-toast {
      z-index: 10002 !important;
    }
  `;
  document.head.appendChild(style);
}
