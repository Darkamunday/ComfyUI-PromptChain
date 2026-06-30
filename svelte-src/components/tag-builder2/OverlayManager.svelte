<script>
  // OverlayManager — "Your edits" review surface for the delta overlay.
  // Lists every user add / edit / delete across all editable tables and lets
  // the user restore a base item (incl. UN-DELETING a tombstoned one, which
  // has no in-grid affordance), remove an added item, or export the whole
  // overlay. Reads /tag-builder/overlay (aggregate) and reuses the existing
  // restore / delete routes.

  let {
    labels = {},          // table -> friendly section label
    onClose = () => {},
    onChanged = () => {},  // fired after any restore/remove so the parent refreshes
  } = $props();

  let loading = $state(true);
  let tables = $state({});
  let busy = $state(false);

  async function load() {
    loading = true;
    try {
      const res = await fetch("/promptchain/tag-builder/overlay", { cache: "no-store" });
      tables = res.ok ? (await res.json()).tables || {} : {};
    } catch {
      tables = {};
    }
    loading = false;
  }
  $effect(() => { load(); });

  let entries = $derived(Object.entries(tables));
  let isEmpty = $derived(!loading && entries.length === 0);

  function labelFor(table) { return labels[table] || table; }
  function editedCols(edit) { return Object.keys(edit).filter(k => k !== "_base_at_edit"); }
  function addName(row) { return row?.display_name || row?.display || row?.item_tag || row?.tag || "(item)"; }

  async function act(method, url) {
    if (busy) return;
    busy = true;
    try {
      const res = await fetch(url, { method });
      if (!res.ok) console.error("[OverlayManager] action failed:", res.status);
    } catch (e) {
      console.error("[OverlayManager] action error", e);
    }
    busy = false;
    await load();
    onChanged();
  }
  const restore = (table, pk) =>
    act("POST", `/promptchain/tag-builder/overlay/${table}/${encodeURIComponent(pk)}/restore`);
  const removeAdd = (table, pk) =>
    act("DELETE", `/promptchain/tag-builder/overlay/${table}/${encodeURIComponent(pk)}`);

  function exportAll() {
    const blob = new Blob([JSON.stringify(tables, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "promptchain-tag-edits.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  function handleKeydown(e) { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }
  function handleOverlayClick(e) { if (e.target === e.currentTarget) onClose(); }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="pcr-atb2-mgr-overlay" onclick={handleOverlayClick} onkeydown={handleKeydown}>
  <div class="pcr-atb2-mgr-modal" onclick={(e) => e.stopPropagation()}>
    <div class="pcr-atb2-mgr-header">
      <span class="pcr-atb2-mgr-title">Your edits</span>
      <div class="pcr-atb2-mgr-header-actions">
        <button class="pcr-atb2-mgr-export" disabled={isEmpty} onclick={exportAll} title="Download your edits as JSON">Export</button>
        <button class="pcr-atb2-mgr-close" onclick={onClose} aria-label="Close">&times;</button>
      </div>
    </div>

    <div class="pcr-atb2-mgr-body">
      {#if loading}
        <div class="pcr-atb2-mgr-empty">Loading…</div>
      {:else if isEmpty}
        <div class="pcr-atb2-mgr-empty">No edits yet. Right-click a chip or character to change it — your edits show up here and survive updates.</div>
      {:else}
        {#each entries as [table, t]}
          <div class="pcr-atb2-mgr-section">
            <div class="pcr-atb2-mgr-section-title">{labelFor(table)}</div>

            {#each Object.entries(t.edits) as [pk, edit]}
              <div class="pcr-atb2-mgr-row">
                <span class="pcr-atb2-mgr-tag pcr-atb2-mgr-badge-edit">edited</span>
                <span class="pcr-atb2-mgr-name">{pk}</span>
                <span class="pcr-atb2-mgr-fields">{editedCols(edit).join(", ")}</span>
                <button class="pcr-atb2-mgr-act" disabled={busy} onclick={() => restore(table, pk)}>Restore</button>
              </div>
            {/each}

            {#each t.deletes as pk}
              <div class="pcr-atb2-mgr-row">
                <span class="pcr-atb2-mgr-tag pcr-atb2-mgr-badge-del">deleted</span>
                <span class="pcr-atb2-mgr-name">{pk}</span>
                <span class="pcr-atb2-mgr-fields"></span>
                <button class="pcr-atb2-mgr-act" disabled={busy} onclick={() => restore(table, pk)}>Bring back</button>
              </div>
            {/each}

            {#each Object.entries(t.adds) as [pk, row]}
              <div class="pcr-atb2-mgr-row">
                <span class="pcr-atb2-mgr-tag pcr-atb2-mgr-badge-add">added</span>
                <span class="pcr-atb2-mgr-name">{addName(row)}</span>
                <span class="pcr-atb2-mgr-fields pcr-atb2-mgr-mono">{pk}</span>
                <button class="pcr-atb2-mgr-act pcr-atb2-mgr-act-danger" disabled={busy} onclick={() => removeAdd(table, pk)}>Remove</button>
              </div>
            {/each}
          </div>
        {/each}
      {/if}
    </div>

    <div class="pcr-atb2-mgr-footer">
      <button class="pcr-atb2-mgr-btn" onclick={onClose}>Done</button>
    </div>
  </div>
</div>

<style>
  .pcr-atb2-mgr-overlay {
    position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); z-index: 100074;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .pcr-atb2-mgr-modal {
    background: var(--pcr-panel, #1a1a1f); color: var(--pcr-text, #e6e6e6);
    border: 1px solid var(--pcr-border, #2a2a32); border-radius: 10px;
    width: 560px; max-width: 100%; max-height: 86vh;
    display: flex; flex-direction: column; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
  }
  .pcr-atb2-mgr-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 14px; border-bottom: 1px solid var(--pcr-border, #2a2a32);
  }
  .pcr-atb2-mgr-title { font-size: 14px; }
  .pcr-atb2-mgr-header-actions { display: flex; align-items: center; gap: 8px; }
  .pcr-atb2-mgr-export {
    padding: 5px 10px; font-size: 12px; border-radius: 6px;
    border: 1px solid var(--pcr-border, #2a2a32); background: rgba(255,255,255,0.04);
    color: inherit; cursor: pointer;
  }
  .pcr-atb2-mgr-export:hover:not(:disabled) { background: rgba(255,255,255,0.08); }
  .pcr-atb2-mgr-export:disabled { opacity: 0.4; cursor: not-allowed; }
  .pcr-atb2-mgr-close { background: transparent; border: 0; color: inherit; font-size: 22px; line-height: 1; cursor: pointer; padding: 0 6px; }
  .pcr-atb2-mgr-body { padding: 8px 14px 14px; overflow-y: auto; }
  .pcr-atb2-mgr-empty { padding: 28px 8px; text-align: center; color: #888; font-size: 13px; line-height: 1.5; }
  .pcr-atb2-mgr-section { margin-top: 12px; }
  .pcr-atb2-mgr-section-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
    color: #9b8cff; padding: 6px 2px 4px; border-bottom: 1px solid var(--pcr-border, #2a2a32); margin-bottom: 4px;
  }
  .pcr-atb2-mgr-row { display: flex; align-items: center; gap: 8px; padding: 6px 2px; font-size: 13px; }
  .pcr-atb2-mgr-tag {
    flex: 0 0 auto; font-size: 10px; text-transform: uppercase; letter-spacing: 0.4px;
    padding: 2px 6px; border-radius: 4px;
  }
  .pcr-atb2-mgr-badge-edit { background: rgba(124, 58, 237, 0.2); color: #c4b5fd; }
  .pcr-atb2-mgr-badge-del { background: rgba(248, 113, 113, 0.18); color: #fca5a5; }
  .pcr-atb2-mgr-badge-add { background: rgba(52, 211, 153, 0.18); color: #6ee7b7; }
  .pcr-atb2-mgr-name { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pcr-atb2-mgr-fields { flex: 0 1 auto; color: #888; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
  .pcr-atb2-mgr-mono { font-family: ui-monospace, "Cascadia Code", monospace; }
  .pcr-atb2-mgr-act {
    flex: 0 0 auto; padding: 4px 10px; font-size: 12px; border-radius: 5px;
    border: 1px solid var(--pcr-border, #2a2a32); background: rgba(255,255,255,0.04);
    color: inherit; cursor: pointer;
  }
  .pcr-atb2-mgr-act:hover:not(:disabled) { background: rgba(124, 58, 237, 0.2); }
  .pcr-atb2-mgr-act-danger:hover:not(:disabled) { background: rgba(248, 113, 113, 0.2); }
  .pcr-atb2-mgr-act:disabled { opacity: 0.5; cursor: not-allowed; }
  .pcr-atb2-mgr-footer { display: flex; justify-content: flex-end; padding: 12px 14px; border-top: 1px solid var(--pcr-border, #2a2a32); }
  .pcr-atb2-mgr-btn {
    padding: 8px 16px; border-radius: 6px; border: 1px solid var(--pcr-border, #2a2a32);
    background: #7c3aed; border-color: #7c3aed; color: #fff; cursor: pointer; font-size: 13px;
  }
  .pcr-atb2-mgr-btn:hover { background: #8b5cf6; }
</style>
