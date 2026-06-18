<script>
  // Cascading new-workflow picker: blank entry + searchable arch-grouped
  // model list; clicking a model snaps a template submenu beside it; clicking
  // a template creates the workflow immediately (no naming step — the parent
  // auto-names and dedupes). Replaces the old Create New Workflow modal.
  import { portal } from "../../lib/portal.js";

  let { open, anchor = { x: 0, y: 0 }, fetchApi, onPick, onClose } = $props();

  let models = $state([]);
  let allConfigs = $state({});
  let loadingModels = $state(false);
  let loadError = $state(null);
  let search = $state("");
  let selectedModelHash = $state("");
  let templates = $state([]);
  let loadingTemplates = $state(false);
  let searchEl = $state(null);
  let menuEl = $state(null);
  const templateCache = new Map(); // "arch|family" -> templates

  $effect(() => {
    if (!open) return;
    search = "";
    selectedModelHash = "";
    templates = [];
    if (models.length === 0 && !loadingModels) loadModels();
    requestAnimationFrame(() => searchEl?.focus());
  });

  async function loadModels() {
    loadingModels = true;
    loadError = null;
    try {
      const resp = await fetchApi("/promptchain/models/list");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const raw = data.models || [];
      const hashes = raw.map(m => m.hash).filter(Boolean);
      if (hashes.length) {
        try {
          const cfgResp = await fetchApi("/promptchain/models/settings/bulk", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hashes }),
          });
          if (cfgResp.ok) allConfigs = (await cfgResp.json()).settings || {};
        } catch (e) { console.error("[PromptChain] bulk settings fetch failed:", e); }
      }
      models = raw.sort((a, b) => displayName(a).localeCompare(displayName(b)));
    } catch (e) {
      console.error("[PromptChain] model list load failed:", e);
      models = [];
      loadError = "Could not load models";
    }
    loadingModels = false;
  }

  function displayName(m) {
    const cfg = allConfigs[m.hash];
    const dn = cfg?.display_name || cfg?.model_name || "";
    return dn || (m.filename || "").replace(/\.(safetensors|ckpt|gguf)$/i, "");
  }

  let archGroups = $derived.by(() => {
    const q = search.trim().toLowerCase();
    const groups = new Map();
    for (const m of models) {
      if (q && !displayName(m).toLowerCase().includes(q)) continue;
      const arch = allConfigs[m.hash]?.architecture || "other";
      if (!groups.has(arch)) groups.set(arch, []);
      groups.get(arch).push(m);
    }
    return [...groups.entries()]
      .sort((a, b) => (a[0] === "other") - (b[0] === "other") || a[0].localeCompare(b[0]))
      .map(([arch, list]) => ({ label: arch === "other" ? "unrecognized" : arch, models: list }));
  });

  let selectedModel = $derived(models.find(m => m.hash === selectedModelHash));

  async function pickModel(m) {
    selectedModelHash = m.hash;
    templates = [];
    const cfg = allConfigs[m.hash];
    if (!cfg) return;
    const key = `${cfg.architecture || ""}|${cfg.family || ""}`;
    if (templateCache.has(key)) {
      templates = templateCache.get(key);
      return;
    }
    loadingTemplates = true;
    const params = new URLSearchParams();
    if (cfg.architecture) params.set("arch", cfg.architecture);
    if (cfg.family) params.set("family", cfg.family);
    try {
      const resp = await fetchApi(`/promptchain/templates/list?${params}`);
      const data = resp.ok ? await resp.json() : { templates: [] };
      const list = (data.templates || []).filter(t => !t._hidden);
      templateCache.set(key, list);
      // a slow earlier fetch must not clobber the submenu of a newer pick
      if (selectedModelHash === m.hash) templates = list;
    } catch { if (selectedModelHash === m.hash) templates = []; }
    loadingTemplates = false;
  }

  function pickTemplate(tpl) {
    const m = selectedModel;
    if (!m) return;
    onPick?.({
      template: tpl,
      modelFilename: m.filename || "",
      suggestedName: `${tpl.name} - ${displayName(m)}`.toLowerCase(),
    });
  }

  const TEMPLATE_BLURBS = {
    "Text-to-Image": "Render images from a written prompt",
    "Text-to-Image 3D": "Prompt plus 3D Poser pose control",
    "Text-to-Image 3D Regional": "3D Poser with per-character regional prompts",
    "Text-to-Image + FaceDetailer": "Prompt render with automatic face cleanup",
    "Image-to-Image": "Re-render an existing image with a prompt",
    "Image Edit": "Change an existing image with edit instructions",
    "Inpaint": "Repaint a masked region of an image",
    "Combine 2 References": "Blend two reference images into one",
    "Combine 3 References": "Blend three reference images into one",
    "Multi-Ref Edit (2)": "Edit using two reference images",
    "Multi-Ref Edit (3)": "Edit using three reference images",
    "Pose Transfer (AnyPose)": "Apply a reference pose to your character",
    "Pose Transfer (RefControl)": "Apply a reference pose to your character",
    "Text-to-Video": "Generate a video clip from a prompt",
    "Image-to-Video": "Animate a still image into a video clip",
  };
  const CATEGORY_BLURBS = {
    Generation: "Generate new images",
    Editing: "Edit existing images",
    Video: "Generate video",
    Custom: "Specialized workflow",
  };
  function tplBlurb(t) {
    return TEMPLATE_BLURBS[t.name] || CATEGORY_BLURBS[t.category] || "";
  }

  // viewport-clamped position; submenu extends to the right of the main column
  let pos = $derived.by(() => {
    const mw = selectedModelHash ? 540 : 280, mh = 440;
    const vw = window.innerWidth, vh = window.innerHeight;
    return {
      left: Math.max(8, Math.min(anchor.x, vw - mw - 8)),
      top: Math.max(8, Math.min(anchor.y, vh - mh - 8)),
    };
  });

  $effect(() => {
    if (!open) return;
    function onClick(e) {
      if (menuEl && !menuEl.contains(e.target)) onClose?.();
    }
    function onKey(e) {
      if (e.key === "Escape") { e.stopPropagation(); onClose?.(); }
    }
    window.addEventListener("click", onClick, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("click", onClick, true);
      window.removeEventListener("keydown", onKey, true);
    };
  });
</script>

{#if open}
  <div use:portal class="pcr-nwm" bind:this={menuEl} style="left:{pos.left}px;top:{pos.top}px;">
    <div class="pcr-nwm-col">
      <button class="pcr-nwm-item pcr-nwm-blank" onclick={() => onPick?.({ blank: true })}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        Blank workflow
      </button>
      <div class="pcr-nwm-sep"></div>
      <input
        bind:this={searchEl}
        bind:value={search}
        type="text"
        class="pcr-nwm-search"
        placeholder="Search models..."
      />
      <div class="pcr-nwm-list">
        {#if loadingModels}
          <div class="pcr-nwm-hint">Loading models...</div>
        {:else if loadError}
          <div class="pcr-nwm-hint pcr-nwm-err">{loadError}</div>
        {:else if models.length === 0}
          <div class="pcr-nwm-hint">
            No models found — add a checkpoint to ComfyUI/models/checkpoints and restart ComfyUI.
          </div>
        {:else if archGroups.length === 0}
          <div class="pcr-nwm-hint">No matches</div>
        {:else}
          {#each archGroups as g (g.label)}
            <div class="pcr-nwm-group">{g.label}</div>
            {#each g.models as m (m.hash)}
              <button
                class="pcr-nwm-item"
                class:selected={selectedModelHash === m.hash}
                onclick={() => pickModel(m)}
              >
                <span class="pcr-nwm-mname">{displayName(m)}</span>
                <span class="pcr-nwm-chevron">&#9656;</span>
              </button>
            {/each}
          {/each}
        {/if}
      </div>
    </div>

    {#if selectedModelHash}
      <div class="pcr-nwm-col pcr-nwm-sub">
        {#if loadingTemplates}
          <div class="pcr-nwm-hint">Loading templates...</div>
        {:else if templates.length === 0}
          <div class="pcr-nwm-hint">No templates for this model</div>
        {:else}
          {#each templates as tpl (tpl.id)}
            <button class="pcr-nwm-tpl" onclick={() => pickTemplate(tpl)}>
              {#if tpl.category === "Video"}
                <svg class="pcr-nwm-tpl-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="2" y="4" width="20" height="16" rx="2"/>
                  <path d="M2 8h20M2 16h20M7 4v16M17 4v16"/>
                </svg>
              {:else if tpl.category === "Editing"}
                <svg class="pcr-nwm-tpl-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>
                </svg>
              {:else if tpl.category === "Custom"}
                <svg class="pcr-nwm-tpl-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>
                  <line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>
                  <line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>
                  <line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>
                </svg>
              {:else}
                <svg class="pcr-nwm-tpl-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="3" y="3" width="18" height="18" rx="2"/>
                  <circle cx="8.5" cy="8.5" r="1.5"/>
                  <path d="M21 15l-5-5L5 21"/>
                </svg>
              {/if}
              <span class="pcr-nwm-tpl-text">
                <span class="pcr-nwm-tpl-name">{tpl.name}</span>
                <span class="pcr-nwm-tpl-desc">{tplBlurb(tpl)}</span>
              </span>
            </button>
          {/each}
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .pcr-nwm {
    position: fixed; z-index: 10001;
    display: flex; align-items: stretch;
    background: rgba(38, 38, 38, 0.92);
    backdrop-filter: blur(16px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
    max-height: 440px;
  }
  .pcr-nwm-col {
    display: flex; flex-direction: column;
    width: 260px; padding: 4px 0;
    min-height: 0;
  }
  .pcr-nwm-sub {
    width: 270px;
    border-left: 1px solid rgba(255, 255, 255, 0.08);
    overflow-y: auto; scrollbar-width: thin;
  }
  .pcr-nwm-list { overflow-y: auto; scrollbar-width: thin; flex: 1; min-height: 0; }

  .pcr-nwm-item {
    display: flex; align-items: center; gap: 8px;
    width: 100%; padding: 7px 12px;
    border: none; background: transparent;
    color: var(--input-text, #ccc); font-size: 12px;
    text-align: left; cursor: pointer; white-space: nowrap;
  }
  .pcr-nwm-item:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-nwm-item.selected { background: rgba(243, 107, 0, 0.12); color: #ff8a25; }
  .pcr-nwm-item svg { width: 14px; height: 14px; flex-shrink: 0; }
  .pcr-nwm-blank { font-weight: 500; }
  .pcr-nwm-mname { overflow: hidden; text-overflow: ellipsis; }
  .pcr-nwm-chevron { margin-left: auto; font-size: 10px; opacity: 0.5; }

  .pcr-nwm-sep { height: 1px; margin: 4px 0; background: rgba(255, 255, 255, 0.08); flex-shrink: 0; }

  .pcr-nwm-search {
    margin: 2px 8px 6px; padding: 6px 8px;
    border: 1px solid var(--border-color, #3a3a3a); border-radius: 4px;
    background: rgba(0, 0, 0, 0.25); color: var(--input-text, #ccc);
    font-size: 12px; outline: none; flex-shrink: 0;
  }
  .pcr-nwm-search:focus { border-color: #dd7634; }
  .pcr-nwm-search::placeholder { color: #666; }

  .pcr-nwm-group {
    padding: 6px 12px 2px; font-size: 10px; font-weight: 600;
    color: #777; text-transform: uppercase; letter-spacing: 0.4px;
  }

  .pcr-nwm-hint {
    padding: 14px 12px; font-size: 11px; color: #888;
    line-height: 1.45; white-space: normal;
  }
  .pcr-nwm-err { color: #c45050; }

  .pcr-nwm-tpl {
    display: flex; align-items: flex-start; gap: 9px;
    width: 100%; padding: 8px 12px;
    border: none; background: transparent;
    color: var(--input-text, #ccc);
    text-align: left; cursor: pointer;
  }
  .pcr-nwm-tpl:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-nwm-tpl-icon { width: 16px; height: 16px; flex-shrink: 0; margin-top: 1px; color: #999; }
  .pcr-nwm-tpl:hover .pcr-nwm-tpl-icon { color: #ff8a25; }
  .pcr-nwm-tpl-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  .pcr-nwm-tpl-name { font-size: 12px; font-weight: 500; }
  .pcr-nwm-tpl-desc { font-size: 10px; color: #888; line-height: 1.35; white-space: normal; }
</style>
