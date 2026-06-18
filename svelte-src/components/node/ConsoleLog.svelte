<script>
  // Console log viewer — streams ComfyUI server logs via WebSocket.
  // Fetches history on mount, subscribes to live events, auto-scrolls.

  import { onDestroy, tick } from "svelte";
  import { api } from "/scripts/api.js";

  let {
    active = false,
  } = $props();

  let containerEl;
  let entries = $state([]);
  let pinToBottom = $state(true);
  let subscribed = false;
  // Monotonic id so keyed iteration can't collide when two log entries
  // share a timestamp + message.
  let _nextEntryId = 0;

  function formatTime(iso) {
    try {
      const d = new Date(iso);
      const h = String(d.getHours()).padStart(2, "0");
      const m = String(d.getMinutes()).padStart(2, "0");
      const s = String(d.getSeconds()).padStart(2, "0");
      return `${h}:${m}:${s}`;
    } catch { return ""; }
  }

  // strip ANSI escape codes for plain-text rendering
  function stripAnsi(text) {
    return text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, "");
  }

  function _tag(e) { return { ...e, _id: _nextEntryId++ }; }

  async function fetchHistory() {
    try {
      const data = await api.getRawLogs();
      if (data.entries?.length) {
        entries = data.entries.map(_tag);
        await tick();
        scrollToBottom();
      }
    } catch (e) { console.error("[PromptChain] log history fetch failed:", e); }
  }

  function onLogEvent(event) {
    const incoming = event.detail?.entries;
    if (!incoming?.length) return;
    entries = [...entries, ...incoming.map(_tag)];
    if (pinToBottom) {
      tick().then(scrollToBottom);
    }
  }

  async function subscribe() {
    if (subscribed) return;
    subscribed = true;
    api.addEventListener("logs", onLogEvent);
    try { await api.subscribeLogs(true); } catch {}
  }

  async function unsubscribe() {
    if (!subscribed) return;
    subscribed = false;
    api.removeEventListener("logs", onLogEvent);
    try { await api.subscribeLogs(false); } catch {}
  }

  function scrollToBottom() {
    if (!containerEl) return;
    containerEl.scrollTop = containerEl.scrollHeight;
  }

  function handleScroll() {
    if (!containerEl) return;
    const atBottom = containerEl.scrollHeight - containerEl.scrollTop - containerEl.clientHeight < 30;
    pinToBottom = atBottom;
  }

  // activate/deactivate based on tab visibility
  let initialized = false;
  $effect(() => {
    if (active && !initialized) {
      initialized = true;
      fetchHistory().then(subscribe);
    }
  });

  onDestroy(() => { unsubscribe(); });
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  bind:this={containerEl}
  class="pcr-console-log pcr-scrollable"
  tabindex="0"
  onscroll={handleScroll}
  onpointerdown={(e) => e.stopPropagation()}
  onmousedown={(e) => e.stopPropagation()}
>
  {#if entries.length === 0}
    <span class="pcr-console-placeholder">No log entries</span>
  {:else}
    {#each entries as entry (entry._id)}
      <div class="pcr-console-line">
        <span class="pcr-console-time">{formatTime(entry.t)}</span>
        <span class="pcr-console-msg">{stripAnsi(entry.m)}</span>
      </div>
    {/each}
  {/if}

  {#if !pinToBottom && entries.length > 0}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="pcr-console-scroll-btn" onclick={() => { pinToBottom = true; scrollToBottom(); }}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
    </div>
  {/if}
</div>

<style>
  .pcr-console-log {
    flex: 1 1 0;
    overflow-y: auto !important;
    overflow-x: hidden;
    min-height: 0;
    padding: 4px 0;
    font-family: Consolas, Menlo, "Liberation Mono", monospace;
    font-size: var(--pcr-output-font-size, 13px);
    color: rgba(255, 255, 255, 0.8);
    cursor: text;
    user-select: text;
    -webkit-user-select: text;
    background: repeating-linear-gradient(45deg, transparent, transparent 10px, #cccccc03 10px, #cccccc03 20px);
    outline: none;
    position: relative;
  }
  .pcr-console-log:focus { outline: none; }
  .pcr-console-placeholder {
    color: rgba(255, 255, 255, 0.4);
    font-style: italic;
    padding: 8px 12px;
    display: block;
  }
  .pcr-console-line {
    display: flex;
    gap: 8px;
    padding: 1px 12px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .pcr-console-line:hover {
    background: rgba(255, 255, 255, 0.03);
  }
  .pcr-console-time {
    color: rgba(255, 255, 255, 0.3);
    flex-shrink: 0;
    font-size: 0.9em;
  }
  .pcr-console-msg {
    flex: 1;
    min-width: 0;
  }
  .pcr-console-log::-webkit-scrollbar { width: 6px; }
  .pcr-console-log::-webkit-scrollbar-track { background: transparent; }
  .pcr-console-log::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.15);
    border-radius: 3px;
  }
  .pcr-console-log::-webkit-scrollbar-thumb:hover {
    background: rgba(255, 255, 255, 0.25);
  }
  .pcr-console-scroll-btn {
    position: sticky;
    bottom: 8px;
    left: 50%;
    transform: translateX(-50%);
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: rgba(79, 195, 247, 0.25);
    border: 1px solid rgba(79, 195, 247, 0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: #4fc3f7;
    transition: background 0.15s;
    z-index: 5;
  }
  .pcr-console-scroll-btn:hover {
    background: rgba(79, 195, 247, 0.4);
  }
</style>
