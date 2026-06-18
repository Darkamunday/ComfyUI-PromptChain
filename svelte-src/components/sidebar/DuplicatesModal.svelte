<script>
  import { getContext } from "svelte";
  import { portal } from "../../lib/portal.js";
  import "./modal-shared.css";

  let { open, scope, path = "", fetchApi, onSubscribeDedup, onClose, onDeleted } = $props();
  const apiURL = getContext("pcr-apiURL");

  let phase = $state("idle");    // idle | scanning | results | deleting | error
  let progress = $state(null);   // { done, total }
  let clusters = $state([]);
  let totalImages = $state(0);
  let checked = $state(new Set());
  let threshold = $state(5);
  let errorMsg = $state("");
  let scanId = 0;

  const THRESHOLDS = [
    { v: 0, label: "Identical only" },
    { v: 2, label: "Strict" },
    { v: 5, label: "Normal" },
    { v: 7, label: "Loose" },
  ];

  $effect(() => {
    if (open) {
      scan();
    } else {
      scanId++;
      phase = "idle";
      clusters = [];
      checked = new Set();
      progress = null;
    }
  });

  async function scan() {
    const id = ++scanId;
    phase = "scanning";
    progress = null;
    errorMsg = "";
    const unsub = onSubscribeDedup?.((d) => { if (id === scanId) progress = d; });
    try {
      const params = new URLSearchParams({ scope, path, threshold: String(threshold) });
      const resp = await fetchApi(`/promptchain/browse/duplicates?${params}`);
      if (id !== scanId) return;
      if (!resp.ok) throw new Error(`scan failed (${resp.status})`);
      const data = await resp.json();
      if (id !== scanId) return;
      clusters = data.clusters;
      totalImages = data.totalImages;
      checked = new Set(clusters.flatMap(c => c.items.filter(i => !i.keep).map(i => i.path)));
      phase = "results";
    } catch (e) {
      if (id !== scanId) return;
      errorMsg = e?.message || "scan failed";
      phase = "error";
    } finally {
      unsub?.();
    }
  }

  function setThreshold(v) {
    threshold = v;
    if (phase !== "scanning") scan();
  }

  function toggleChecked(p) {
    const next = new Set(checked);
    next.has(p) ? next.delete(p) : next.add(p);
    checked = next;
  }

  function thumbSrc(item) {
    return apiURL(`/promptchain/browse/preview?scope=${scope}&path=${encodeURIComponent(item.path)}&thumb=1`);
  }

  function fmtSize(b) {
    if (!b) return "-";
    if (b < 1048576) return (b / 1024).toFixed(0) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }

  let dupCount = $derived(clusters.reduce((a, c) => a + c.items.length - 1, 0));

  async function deleteChecked() {
    if (!checked.size || phase === "deleting") return;
    phase = "deleting";
    try {
      const resp = await fetchApi("/promptchain/browse/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, paths: [...checked] }),
      });
      const result = await resp.json();
      const deleted = new Set(result.deleted || []);
      if (deleted.size) {
        window.dispatchEvent(new CustomEvent("promptchain:file-deleted", {
          detail: { scope, paths: [...deleted] },
        }));
        onDeleted?.([...deleted]);
      }
      clusters = clusters
        .map(c => ({ items: c.items.filter(i => !deleted.has(i.path)) }))
        .filter(c => c.items.length >= 2);
      checked = new Set([...checked].filter(p => !deleted.has(p)));
    } catch { /* keep current state visible */ }
    phase = "results";
  }

  function handleKeydown(e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose?.(); }
  }

  function handleBackdrop(e) {
    if (e.target === e.currentTarget) onClose?.();
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div use:portal class="pcr-modal-backdrop" onclick={handleBackdrop} onkeydown={handleKeydown}>
    <div class="pcr-modal pcr-dup-modal" role="dialog" aria-modal="true">
      <div class="pcr-modal-header">
        <span class="pcr-modal-title">Find Duplicates{path ? ` — ${path}` : ""}</span>
        <div class="pcr-dup-thresholds">
          {#each THRESHOLDS as t}
            <button
              class="pcr-dup-th" class:active={threshold === t.v}
              disabled={phase === "scanning"}
              onclick={() => setThreshold(t.v)}
            >{t.label}</button>
          {/each}
        </div>
        <button class="pcr-modal-close" onclick={onClose} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div class="pcr-modal-body pcr-dup-body">
        {#if phase === "scanning" || phase === "deleting"}
          <div class="pcr-dup-progress">
            {#if phase === "deleting"}
              <span>Deleting…</span>
            {:else if progress}
              <span>Hashing images… {progress.done} / {progress.total}</span>
              <div class="pcr-dup-bar"><div class="pcr-dup-bar-fill" style="width:{(progress.done / Math.max(1, progress.total)) * 100}%"></div></div>
            {:else}
              <span>Scanning…</span>
            {/if}
          </div>
        {:else if phase === "error"}
          <div class="pcr-dup-progress pcr-dup-error">{errorMsg}</div>
        {:else if phase === "results" && clusters.length === 0}
          <div class="pcr-dup-progress">No duplicates found across {totalImages} images.</div>
        {:else if phase === "results"}
          <div class="pcr-dup-summary">
            {clusters.length} group{clusters.length === 1 ? "" : "s"} · {dupCount} duplicate{dupCount === 1 ? "" : "s"} across {totalImages} images — largest file in each group is kept by default
          </div>
          <div class="pcr-dup-list">
            {#each clusters as cluster, ci (cluster.items[0]?.path ?? ci)}
              <div class="pcr-dup-cluster">
                {#each cluster.items as item (item.path)}
                  {@const isChecked = checked.has(item.path)}
                  <!-- svelte-ignore a11y_click_events_have_key_events -->
                  <!-- svelte-ignore a11y_no_static_element_interactions -->
                  <div class="pcr-dup-card" class:marked={isChecked} onclick={() => toggleChecked(item.path)}>
                    <div class="pcr-dup-imgwrap">
                      <img src={thumbSrc(item)} alt={item.name} loading="lazy" decoding="async" />
                      {#if item.keep}<span class="pcr-dup-keep">keep</span>{/if}
                      <span class="pcr-dup-check" class:on={isChecked}></span>
                    </div>
                    <div class="pcr-dup-meta" title={item.path}>
                      <span class="pcr-dup-name">{item.name}</span>
                      <span class="pcr-dup-info">{item.width && item.height ? `${item.width}×${item.height} · ` : ""}{fmtSize(item.size)}</span>
                    </div>
                  </div>
                {/each}
              </div>
            {/each}
          </div>
        {/if}
      </div>

      <div class="pcr-modal-footer">
        <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={onClose}>Close</button>
        <button
          class="pcr-modal-btn pcr-modal-btn-danger"
          disabled={!checked.size || phase !== "results"}
          onclick={deleteChecked}
        >Delete {checked.size} file{checked.size === 1 ? "" : "s"}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  :global(.pcr-dup-modal) {
    max-width: min(880px, 94vw) !important;
    width: min(880px, 94vw);
    display: flex; flex-direction: column;
    max-height: 86vh;
  }
  .pcr-dup-body {
    flex: 1; min-height: 120px; overflow-y: auto;
    scrollbar-width: thin;
  }
  .pcr-dup-thresholds { display: flex; gap: 4px; margin: 0 12px; }
  .pcr-dup-th {
    padding: 3px 10px; border: 1px solid #3a3a3a; border-radius: 10px;
    background: transparent; color: #999; font-size: 11px; cursor: pointer;
  }
  .pcr-dup-th:hover { color: #fff; }
  .pcr-dup-th.active { color: #ff8a25; border-color: rgba(243, 107, 0, 0.6); background: rgba(243, 107, 0, 0.1); }
  .pcr-dup-th:disabled { opacity: 0.5; cursor: default; }

  .pcr-dup-progress {
    display: flex; flex-direction: column; gap: 10px;
    align-items: center; justify-content: center;
    min-height: 100px; color: #aaa; font-size: 13px;
  }
  .pcr-dup-error { color: #c45050; }
  .pcr-dup-bar {
    width: 70%; height: 6px; border-radius: 3px;
    background: rgba(255, 255, 255, 0.08); overflow: hidden;
  }
  .pcr-dup-bar-fill { height: 100%; background: #f36b00; transition: width 0.2s; }

  .pcr-dup-summary { font-size: 12px; color: #999; margin-bottom: 12px; }

  .pcr-dup-list { display: flex; flex-direction: column; gap: 10px; }
  .pcr-dup-cluster {
    display: flex; gap: 8px; padding: 8px;
    border: 1px solid rgba(255, 255, 255, 0.07); border-radius: 6px;
    overflow-x: auto; scrollbar-width: thin;
  }
  .pcr-dup-card {
    flex: 0 0 132px; cursor: pointer;
    border: 2px solid transparent; border-radius: 4px; padding: 3px;
  }
  .pcr-dup-card:hover { border-color: rgba(255, 255, 255, 0.15); }
  .pcr-dup-card.marked { border-color: rgba(180, 42, 42, 0.7); background: rgba(180, 42, 42, 0.08); }
  .pcr-dup-imgwrap {
    position: relative; width: 100%; aspect-ratio: 1;
    border-radius: 3px; overflow: hidden; background: rgba(0, 0, 0, 0.25);
  }
  .pcr-dup-imgwrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .pcr-dup-keep {
    position: absolute; top: 4px; left: 4px;
    padding: 1px 6px; border-radius: 7px;
    background: rgba(36, 120, 50, 0.9); color: #fff; font-size: 9px;
  }
  .pcr-dup-check {
    position: absolute; top: 4px; right: 4px;
    width: 15px; height: 15px; border-radius: 3px;
    border: 1.5px solid rgba(255, 255, 255, 0.6);
    background: rgba(0, 0, 0, 0.4);
  }
  .pcr-dup-check.on {
    background: #b42a2a; border-color: #b42a2a;
  }
  .pcr-dup-check.on::after {
    content: ""; position: absolute; left: 4px; top: 1px;
    width: 4px; height: 8px;
    border: solid #fff; border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .pcr-dup-meta { display: flex; flex-direction: column; margin-top: 3px; }
  .pcr-dup-name {
    font-size: 10px; color: #ccc;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pcr-dup-info { font-size: 9px; color: #777; }
</style>
