<script>
  // Topbar — matches vanilla fullscreen-editor.js structure exactly.
  // Panel toggle buttons and execution state are wired imperatively by the bridge
  // after mount (querySelector on the rendered DOM), same pattern as vanilla.

  let {
    logoUrl = "",
    workflowName = "Workflow",
    onQueuePrompt = () => {},
    onCancelExecution = () => {},
    onClose = () => {},
  } = $props();

  let batchCount = $state(1);
</script>

<div class="pcr-fs-topbar">
  <div class="pcr-fs-topbar-left">
    {#if logoUrl}
      <img class="pcr-fs-topbar-logo" src={logoUrl} alt="PromptChain" />
    {/if}
    <span class="pcr-fs-topbar-title" data-name={workflowName}></span>
  </div>

  <div class="pcr-fs-topbar-right">
    <div class="pcr-fs-topbar-actions">
      <input
        type="number"
        class="pcr-fs-batch-input"
        bind:value={batchCount}
        min="1"
        max="99"
        title="Batch count"
      />
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-fs-run-btn" title="Queue prompt (Ctrl+Enter)" onclick={(e) => {
        e.stopPropagation();
        onQueuePrompt(batchCount);
      }}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        <span>Run</span>
      </span>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <span class="pcr-fs-cancel-btn pcr-fs-inactive" title="Cancel execution" onclick={(e) => {
        e.stopPropagation();
        onCancelExecution();
      }}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
      </span>
      <span class="pcr-fs-queue-badge pcr-fs-inactive">0 active</span>
    </div>

    <div class="pcr-fs-panel-toggles">
      <span class="pcr-fs-header-btn" title="Toggle 3D Poser panel" data-fs-action="pose">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20.5 6c-2.61.7-5.67 1-8.5 1s-5.89-.3-8.5-1L3 8c1.86.5 4 .83 6 1v13h2v-6h2v6h2V9c2-.17 4.14-.5 6-1l-.5-2zM12 6c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2z"/></svg>
      </span>
      <span class="pcr-fs-header-btn" title="Toggle output panel" data-fs-action="output">
        <svg width="16" height="16" viewBox="0 -960 960 960" fill="currentColor"><path d="M400-280h160v-80H400v80Zm0-160h280v-80H400v80ZM280-600h400v-80H280v80Zm200 120ZM80-80v-80h102q-48-23-77.5-68T75-330q0-79 55.5-134.5T265-520v80q-45 0-77.5 32T155-330q0 39 24 69t61 38v-97h80v240H80Zm320-40v-80h360v-560H200v160h-80v-160q0-33 23.5-56.5T200-840h560q33 0 56.5 23.5T840-760v560q0 33-23.5 56.5T760-120H400Z"/></svg>
      </span>
      <span class="pcr-fs-header-btn" title="Toggle image preview" data-fs-action="image">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>
      </span>
    </div>

    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <span class="pcr-fs-close-btn" title="Close (Escape)" onclick={(e) => {
      e.stopPropagation();
      onClose();
    }}>{"\u2715"}</span>
  </div>
</div>

<style>
  .pcr-fs-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 6px;
    height: 42px;
    flex-shrink: 0;
    background: var(--pcr-fs-chrome-surface);
    border-bottom: 1px solid #3c3c3c;
  }
  .pcr-fs-topbar-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .pcr-fs-topbar-logo {
    height: 26px;
    margin-top: 2px;
  }
  .pcr-fs-topbar-title {
    font-size: 13px;
    color: #aeaeae;
    background: #171717;
    border: 1px solid #2f2f2f;
    border-radius: 5px;
    padding: 0px 10px 4px 10px;
    margin-left: 10px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pcr-fs-topbar-title::after { content: attr(data-name); }
  .pcr-fs-topbar-right {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .pcr-fs-topbar-actions {
    display: flex;
    align-items: center;
    gap: 5px;
    background: #2a2a2a;
    border-radius: 6px;
    padding: 3px 4px;
  }
  .pcr-fs-batch-input {
    width: 32px;
    height: 24px;
    background: #1a1a1a;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    color: #ccc;
    font-size: 12px;
    text-align: center;
    outline: none;
    -moz-appearance: textfield;
  }
  .pcr-fs-batch-input::-webkit-inner-spin-button { opacity: 0; width: 12px; }
  .pcr-fs-batch-input:hover::-webkit-inner-spin-button { opacity: 1; }
  .pcr-fs-batch-input:focus { border-color: #4fc3f7; }
  .pcr-fs-run-btn {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 2px 12px 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    color: #fff;
    font-size: 12px;
    font-weight: 600;
    background: #2563eb;
    transition: background 0.15s;
  }
  .pcr-fs-run-btn :global(svg) { margin-top: 1px; }
  .pcr-fs-run-btn:hover { background: #3b82f6; }
  :global(.pcr-fs-run-active) { background: #1d4ed8; }
  .pcr-fs-cancel-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 4px;
    cursor: pointer;
    color: #fff;
    background: #7f1d1d;
    transition: background 0.15s;
  }
  .pcr-fs-cancel-btn:hover { background: #991b1b; }
  .pcr-fs-queue-badge {
    font-size: 12px;
    color: #ccc;
    padding: 4px 8px;
    white-space: nowrap;
    min-width: 56px;
    text-align: center;
  }
  :global(.pcr-fs-inactive) {
    opacity: 0.4;
    pointer-events: none;
  }
  .pcr-fs-close-btn {
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
    color: #888;
    font-size: 16px;
    transition: background 0.1s, color 0.1s;
  }
  .pcr-fs-close-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #fff;
  }
  .pcr-fs-panel-toggles {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 0 8px;
    flex-shrink: 0;
  }
  .pcr-fs-header-btn {
    cursor: pointer;
    padding: 4px 6px;
    border-radius: 4px;
    color: #888;
    display: flex;
    align-items: center;
    transition: background 0.1s, color 0.1s;
  }
  .pcr-fs-header-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #fff;
  }
</style>
