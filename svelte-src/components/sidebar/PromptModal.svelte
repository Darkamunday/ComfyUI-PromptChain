<script>
  import { portal } from "../../lib/portal.js";
  import "./modal-shared.css";

  let { open, title, placeholder, defaultValue = "", selectEnd = -1, confirmLabel = "Create", onConfirm, onCancel } = $props();

  let value = $state("");
  let inputEl = $state(null);

  $effect(() => {
    if (open) {
      value = defaultValue;
      requestAnimationFrame(() => {
        inputEl?.focus();
        if (selectEnd >= 0) inputEl?.setSelectionRange(0, selectEnd);
        else inputEl?.select();
      });
    }
  });

  function handleConfirm() {
    if (value.trim()) {
      onConfirm?.(value.trim());
      value = "";
    }
  }

  function handleCancel() {
    value = "";
    onCancel?.();
  }

  function handleKeydown(e) {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); handleConfirm(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); handleCancel(); }
  }

  function handleBackdrop(e) {
    if (e.target === e.currentTarget) handleCancel();
  }

</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div use:portal class="pcr-modal-backdrop" onclick={handleBackdrop} onkeydown={handleKeydown}>
    <div class="pcr-modal" role="dialog" aria-modal="true">
      <div class="pcr-modal-header">
        <span class="pcr-modal-title">{title}</span>
        <button class="pcr-modal-close" onclick={handleCancel} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
      <div class="pcr-modal-body">
        <input
          bind:this={inputEl}
          bind:value
          type="text"
          class="pcr-modal-input"
          placeholder={placeholder}
          onkeydown={handleKeydown}
        />
      </div>
      <div class="pcr-modal-footer">
        <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={handleCancel}>Cancel</button>
        <button class="pcr-modal-btn pcr-modal-btn-primary" onclick={handleConfirm} disabled={!value.trim()}>{confirmLabel}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .pcr-modal-input {
    width: 100%; padding: 10px 12px;
    background: #1a1a1a; border: 1px solid #3a3a3a; border-radius: 6px;
    color: #fff; font-size: 14px; outline: none; box-sizing: border-box;
  }
  .pcr-modal-input:focus { border-color: #dd7634; }
  .pcr-modal-input::placeholder { color: #666; }
</style>
