<script>
  import { portal } from "../../lib/portal.js";
  import "./modal-shared.css";

  let { open, title = "Confirm", message = "", confirmLabel = "Delete", onConfirm, onCancel } = $props();

  let confirmBtn = $state(null);

  $effect(() => {
    if (open) requestAnimationFrame(() => confirmBtn?.focus());
  });

  function handleKeydown(e) {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); onConfirm?.(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel?.(); }
  }

  function handleBackdrop(e) {
    if (e.target === e.currentTarget) onCancel?.();
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div use:portal class="pcr-modal-backdrop" onclick={handleBackdrop} onkeydown={handleKeydown}>
    <div class="pcr-modal" role="dialog" aria-modal="true">
      <div class="pcr-modal-header">
        <span class="pcr-modal-title">{title}</span>
        <button class="pcr-modal-close" onclick={onCancel} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
      <div class="pcr-modal-body">
        <p class="pcr-cf-msg">{message}</p>
      </div>
      <div class="pcr-modal-footer">
        <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={onCancel}>Cancel</button>
        <button class="pcr-modal-btn pcr-modal-btn-danger" bind:this={confirmBtn} onclick={onConfirm}>{confirmLabel}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .pcr-cf-msg { margin: 0; font-size: 13px; color: var(--input-text, #ccc); line-height: 1.4; }
</style>
