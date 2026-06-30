<script>
  // TagBuilderEditor — one generic add/edit form for any user-editable
  // tag-builder entity. Driven by a `fields` descriptor so the same modal
  // edits items today and groups/vocab/characters later. Writes go to the
  // delta-overlay routes (/promptchain/tag-builder/overlay/...), never the
  // base DB, so edits survive auto-updates. The parent re-syncs its browse
  // cache from the merged read on confirm.

  let {
    bucket,
    table,                 // overlay entity table, e.g. "appearance_items"
    mode = "edit",         // "add" | "edit"
    fields = [],           // [{ key, label, type, required, mono, pkField }]
    initial = {},
    groups = [],           // [{ group_name, display_name }] for type:"group"
    onConfirm = () => {},
    onCancel = () => {},
  } = $props();

  let row = $state({ ...initial });
  let saving = $state(false);
  let error = $state("");

  let pkKey = $derived(fields.find(f => f.pkField)?.key || "item_tag");
  let title = $derived((mode === "add" ? "Add" : "Edit") + " " + (bucket || "item"));

  async function save() {
    error = "";
    for (const f of fields) {
      if (f.required && !String(row[f.key] ?? "").trim()) {
        error = `${f.label} is required`;
        return;
      }
    }
    const body = {};
    for (const f of fields) {
      let v = row[f.key];
      if (f.type === "number") v = (v === "" || v == null) ? null : Number(v);
      body[f.key] = v ?? (f.type === "number" ? null : "");
    }
    saving = true;
    try {
      let res;
      if (mode === "add") {
        res = await fetch(`/promptchain/tag-builder/overlay/${table}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        const pk = encodeURIComponent(initial[pkKey] ?? "");
        res = await fetch(`/promptchain/tag-builder/overlay/${table}/${pk}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      if (!res.ok) {
        let msg = `Save failed (${res.status})`;
        try { const j = await res.json(); if (j.error) msg = j.error; } catch {}
        error = msg;
        saving = false;
        return;
      }
      onConfirm({ bucket, table, mode });
    } catch (e) {
      error = String(e);
      saving = false;
    }
  }

  function handleKeydown(e) {
    if (e.key === "Escape") { e.stopPropagation(); onCancel(); }
  }
  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) onCancel();
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="pcr-atb2-ed-overlay" onclick={handleOverlayClick} onkeydown={handleKeydown}>
  <div class="pcr-atb2-ed-modal" onclick={(e) => e.stopPropagation()}>
    <div class="pcr-atb2-ed-header">
      <span class="pcr-atb2-ed-title">{title}</span>
      <button class="pcr-atb2-ed-close" onclick={onCancel} aria-label="Close">&times;</button>
    </div>

    <div class="pcr-atb2-ed-body">
      {#each fields as f}
        {@const readonly = f.pkField && mode === "edit"}
        <label class="pcr-atb2-ed-row">
          <span class="pcr-atb2-ed-label">
            {f.label}{#if f.required}<span class="pcr-atb2-ed-req">*</span>{/if}
          </span>
          {#if f.type === "textarea"}
            <textarea class="pcr-atb2-ed-input" rows="2" bind:value={row[f.key]}></textarea>
          {:else if f.type === "group"}
            <select class="pcr-atb2-ed-input" bind:value={row[f.key]}>
              {#each groups as g}
                <option value={g.group_name}>{g.display_name || g.group_name}</option>
              {/each}
            </select>
          {:else if f.type === "number"}
            <input class="pcr-atb2-ed-input" type="number" bind:value={row[f.key]} />
          {:else}
            <input
              class="pcr-atb2-ed-input"
              class:pcr-atb2-ed-mono={f.mono}
              type="text"
              readonly={readonly}
              bind:value={row[f.key]}
            />
          {/if}
          {#if readonly}
            <span class="pcr-atb2-ed-hint">ID can't change — delete &amp; re-add to rename</span>
          {/if}
        </label>
      {/each}

      {#if error}
        <div class="pcr-atb2-ed-error">{error}</div>
      {/if}
    </div>

    <div class="pcr-atb2-ed-footer">
      <button class="pcr-atb2-ed-btn pcr-atb2-ed-cancel" onclick={onCancel}>Cancel</button>
      <button class="pcr-atb2-ed-btn pcr-atb2-ed-ok" disabled={saving} onclick={save}>
        {saving ? "Saving…" : (mode === "add" ? "Add" : "Save")}
      </button>
    </div>
  </div>
</div>

<style>
  .pcr-atb2-ed-overlay {
    position: fixed; inset: 0;
    background: rgba(0, 0, 0, 0.6);
    z-index: 100075;
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
  }
  .pcr-atb2-ed-modal {
    background: var(--pcr-panel, #1a1a1f);
    color: var(--pcr-text, #e6e6e6);
    border: 1px solid var(--pcr-border, #2a2a32);
    border-radius: 10px;
    width: 420px; max-width: 100%; max-height: 90vh;
    display: flex; flex-direction: column;
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
  }
  .pcr-atb2-ed-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 14px;
    border-bottom: 1px solid var(--pcr-border, #2a2a32);
  }
  .pcr-atb2-ed-title { font-size: 14px; text-transform: capitalize; }
  .pcr-atb2-ed-close {
    background: transparent; border: 0; color: inherit;
    font-size: 22px; line-height: 1; cursor: pointer; padding: 0 6px;
  }
  .pcr-atb2-ed-body {
    padding: 14px; display: flex; flex-direction: column; gap: 10px;
    overflow-y: auto;
  }
  .pcr-atb2-ed-row { display: flex; flex-direction: column; gap: 4px; }
  .pcr-atb2-ed-label { font-size: 12px; color: #b9b9c4; }
  .pcr-atb2-ed-req { color: #f87171; margin-left: 2px; }
  .pcr-atb2-ed-input {
    width: 100%; box-sizing: border-box;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--pcr-border, #2a2a32);
    border-radius: 6px;
    color: inherit; font-size: 13px;
    resize: vertical;
    /* Render the native <select> popup dark instead of light-on-white. */
    color-scheme: dark;
  }
  /* Belt-and-suspenders for browsers that honor option colors directly. */
  .pcr-atb2-ed-input option {
    background-color: var(--pcr-panel, #1a1a1f);
    color: var(--pcr-text, #e6e6e6);
  }
  .pcr-atb2-ed-input:focus { outline: none; border-color: #7c3aed; }
  .pcr-atb2-ed-input[readonly] { opacity: 0.6; cursor: not-allowed; }
  .pcr-atb2-ed-mono { font-family: ui-monospace, "Cascadia Code", monospace; }
  .pcr-atb2-ed-hint { font-size: 11px; color: #777; }
  .pcr-atb2-ed-error {
    padding: 8px 10px; border-radius: 6px;
    background: rgba(248, 113, 113, 0.12);
    border: 1px solid rgba(248, 113, 113, 0.4);
    color: #fca5a5; font-size: 12px;
  }
  .pcr-atb2-ed-footer {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 12px 14px;
    border-top: 1px solid var(--pcr-border, #2a2a32);
  }
  .pcr-atb2-ed-btn {
    padding: 8px 14px; border-radius: 6px;
    border: 1px solid var(--pcr-border, #2a2a32);
    background: rgba(255, 255, 255, 0.04);
    color: inherit; cursor: pointer; font-size: 13px;
  }
  .pcr-atb2-ed-cancel:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-atb2-ed-ok { background: #7c3aed; border-color: #7c3aed; color: #fff; }
  .pcr-atb2-ed-ok:hover:not(:disabled) { background: #8b5cf6; }
  .pcr-atb2-ed-ok:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
