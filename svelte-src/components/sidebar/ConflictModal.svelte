<script>
  import { portal } from "../../lib/portal.js";
  import "./modal-shared.css";

  let { open, conflicts = [], total = 0, onConfirm, onCancel } = $props();

  let resolution = $state("rename");
  let confirmBtn = $state(null);

  $effect(() => {
    if (open) {
      resolution = "rename";
      requestAnimationFrame(() => confirmBtn?.focus());
    }
  });

  function handleConfirm() {
    onConfirm?.(resolution);
  }

  function handleCancel() {
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
        <span class="pcr-modal-title">File Conflict</span>
        <button class="pcr-modal-close" onclick={handleCancel} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
      <div class="pcr-modal-body">
        <p class="pcr-cf-msg">
          {conflicts.length} of {total} item{total === 1 ? "" : "s"} already exist in the destination.
        </p>
        <div class="pcr-cf-options">
          <label class="pcr-cf-opt" class:active={resolution === "replace"}>
            <input type="radio" name="conflict" value="replace" bind:group={resolution} />
            <div>
              <div class="pcr-cf-opt-title">Replace</div>
              <div class="pcr-cf-opt-desc">Overwrite existing files</div>
            </div>
          </label>
          <label class="pcr-cf-opt" class:active={resolution === "skip"}>
            <input type="radio" name="conflict" value="skip" bind:group={resolution} />
            <div>
              <div class="pcr-cf-opt-title">Skip</div>
              <div class="pcr-cf-opt-desc">Keep existing, skip conflicts</div>
            </div>
          </label>
          <label class="pcr-cf-opt" class:active={resolution === "rename"}>
            <input type="radio" name="conflict" value="rename" bind:group={resolution} />
            <div>
              <div class="pcr-cf-opt-title">Keep Both</div>
              <div class="pcr-cf-opt-desc">Rename new files automatically</div>
            </div>
          </label>
        </div>
      </div>
      <div class="pcr-modal-footer">
        <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={handleCancel}>Cancel</button>
        <button class="pcr-modal-btn pcr-modal-btn-primary" bind:this={confirmBtn} onclick={handleConfirm}>Continue</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .pcr-cf-msg {
    margin: 0 0 16px; font-size: 13px;
    color: var(--input-text, #ccc); line-height: 1.4;
  }
  .pcr-cf-options { display: flex; flex-direction: column; gap: 8px; }
  .pcr-cf-opt {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; border: 1px solid #3a3a3a; border-radius: 6px;
    cursor: pointer; transition: border-color 0.15s;
  }
  .pcr-cf-opt:hover { border-color: #555; }
  .pcr-cf-opt.active { border-color: #973f00; background: rgba(151, 63, 0, 0.1); }
  .pcr-cf-opt input[type="radio"] { accent-color: #973f00; margin: 0; flex-shrink: 0; }
  .pcr-cf-opt-title { font-size: 13px; font-weight: 500; color: #fff; }
  .pcr-cf-opt-desc { font-size: 11px; color: #888; margin-top: 2px; }
</style>
