<script>
  import { getContext } from "svelte";
  import { portal } from "../../lib/portal.js";
  import "./modal-shared.css";

  let { open, scope, itemPath, onClose } = $props();
  const apiURL = getContext("pcr-apiURL");
  const toast = getContext("pcr-toast");

  let props = $state(null);
  let loading = $state(false);

  $effect(() => {
    if (open && itemPath) {
      loading = true;
      props = null;
      const params = new URLSearchParams({ scope, path: itemPath });
      fetch(apiURL(`/promptchain/browse/properties?${params}`))
        .then(async r => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .then(data => { props = data; loading = false; })
        .catch(e => {
          console.error("[PromptChain] properties load failed:", e);
          toast?.("error", "Failed to load properties");
          loading = false;
          onClose?.();
        });
    }
  });

  function fmtDate(ts) {
    if (!ts) return "-";
    return new Date(ts * 1000).toLocaleString();
  }

  function handleBackdrop(e) {
    if (e.target === e.currentTarget) onClose?.();
  }

  function handleKeydown(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose?.(); }
  }

</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div use:portal class="pcr-modal-backdrop" onclick={handleBackdrop} onkeydown={handleKeydown}>
    <div class="pcr-modal" role="dialog" aria-modal="true">
      <div class="pcr-modal-header">
        <span class="pcr-modal-title">Properties</span>
        <button class="pcr-modal-close" onclick={onClose} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
      <div class="pcr-modal-body">
        {#if loading}
          <div class="pcr-prop-loading">Loading...</div>
        {:else if props}
          <div class="pcr-prop-rows">
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Name</span>
              <span class="pcr-prop-value">{props.name}</span>
            </div>
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Type</span>
              <span class="pcr-prop-value">{props.type}</span>
            </div>
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Path</span>
              <span class="pcr-prop-value">{props.path || "/"}</span>
            </div>
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Full Path</span>
              <span class="pcr-prop-value pcr-prop-selectable">{props.fullPath}</span>
            </div>
            {#if props.size !== undefined}
              <div class="pcr-prop-row">
                <span class="pcr-prop-label">Size</span>
                <span class="pcr-prop-value">{props.sizeFormatted}</span>
              </div>
            {/if}
            {#if props.width && props.height}
              <div class="pcr-prop-row">
                <span class="pcr-prop-label">Dimensions</span>
                <span class="pcr-prop-value">{props.width} &times; {props.height}</span>
              </div>
            {/if}
            {#if props.childCount !== undefined}
              <div class="pcr-prop-row">
                <span class="pcr-prop-label">Items</span>
                <span class="pcr-prop-value">{props.childCount}</span>
              </div>
            {/if}
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Created</span>
              <span class="pcr-prop-value">{fmtDate(props.created)}</span>
            </div>
            <div class="pcr-prop-row">
              <span class="pcr-prop-label">Modified</span>
              <span class="pcr-prop-value">{fmtDate(props.modified)}</span>
            </div>
          </div>
        {/if}
      </div>
      <div class="pcr-modal-footer">
        <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={onClose}>Close</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .pcr-prop-loading { color: #888; font-size: 13px; }
  .pcr-prop-rows { display: flex; flex-direction: column; gap: 8px; }
  .pcr-prop-row { display: flex; gap: 12px; }
  .pcr-prop-label {
    width: 75px; flex-shrink: 0;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px;
    color: #888; padding-top: 1px;
  }
  .pcr-prop-value {
    flex: 1; font-size: 12px; color: #ddd;
    word-break: break-all; line-height: 1.4;
  }
  .pcr-prop-selectable { user-select: text; cursor: text; }
</style>
