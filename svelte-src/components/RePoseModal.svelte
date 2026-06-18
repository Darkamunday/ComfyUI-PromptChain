<script>
  // Re-pose modal — three panes: a detached 3D poser (left), the source image /
  // live render (middle), and the recipe/model/prompt config (right). Pose a
  // throwaway mannequin; pick a recipe whose pose LoRA/CLIP/VAE are installed +
  // a base model; Run renders in the BACKGROUND (the user's workflow is never
  // touched) and the result lands as a lineage child. Uses the shared modal
  // theme (modal-shared.css) + the upscale modal's sizing so it reads identically.
  import { untrack } from "svelte";
  import { portal } from "../lib/portal.js";
  import "./sidebar/modal-shared.css";

  let {
    open = false,
    sourceUrl = "",
    width = 0,
    height = 0,
    caps = null,                // { recipes: [...] } from fetchReposeCaps
    progress = null,            // background tracker state
    onMountPoser = null,        // (el, {width,height,outputMode}) => Promise<handle>
    mountPromptEditor = null,   // (container, initialValue, onChange, modelInfoFn) => Promise<EditorView> — shared rich editor (inpaint/upscale)
    onRun = null,               // (opts) => void
    onUseInEdit = null,         // (doneState) => void — add the result into the editor as a layer (inpaint/upscale pattern)
    onCancel = () => {},
  } = $props();

  const recipes = $derived(caps?.recipes || []);
  let selectedRecipeId = $state("");
  const recipe = $derived(recipes.find((r) => r.id === selectedRecipeId) || null);

  let modelFilename = $state("");
  // One rich editor holds the whole prompt as a PromptChain doc (`//` sections,
  // optional `Negative Prompt:` split, tags) — sent RAW to the background runner,
  // which feeds it to a PromptChain_PromptChain node for server-side compile (the
  // same single source of truth as the inpaint/upscale editors). No client parse.
  let promptDoc = $state("");
  let seed = $state(0);
  let randomizeSeed = $state(true);
  let steps = $state(20);
  let cfg = $state(5);
  let loraStrength = $state(0.7);   // pose-LoRA weight (the "pattern" strength)
  let megapixels = $state(1.0);     // Qwen input-image scale target (AnyPose only)

  let poserEl = null;
  let poserHandle = null;

  // Default the recipe to the first installable one when the modal opens.
  $effect(() => {
    if (!open) return;
    if (!selectedRecipeId || !recipes.some((r) => r.id === selectedRecipeId)) {
      const first = recipes.find((r) => r.ok) || recipes[0];
      if (first) untrack(() => applyRecipeDefaults(first));
    }
  });

  function recipeDoc(r) {
    return r?.promptDoc || "";
  }

  function applyRecipeDefaults(r) {
    selectedRecipeId = r.id;
    promptDoc = recipeDoc(r);
    steps = r.sampler?.steps ?? 20;
    cfg = r.sampler?.cfg ?? 5;
    loraStrength = r.loraStrength ?? 0.7;
    megapixels = r.megapixels ?? 1.0;
    modelFilename = r.models?.[0]?.filename || "";
  }

  function onRecipeChange(e) {
    const r = recipes.find((x) => x.id === e.target.value);
    if (r) applyRecipeDefaults(r);
  }

  // The picked base model identifies the editor's autocomplete/templates (read
  // live when the menu opens, like the upscale modal).
  function editorModelInfo() {
    const m = (recipe?.models || []).find((x) => x.filename === modelFilename);
    return m ? { hash: m.hash, architecture: m.architecture } : null;
  }

  // Mount the shared rich editor (tag highlighting + autocomplete). Seeded once;
  // the {#key selectedRecipeId} wrapper remounts it on a recipe switch so the new
  // recipe's doc loads (mirrors the upscale modal's editor lifecycle).
  function promptEditor(node) {
    let disposed = false, view = null;
    (async () => {
      if (!mountPromptEditor) return;
      const v = await mountPromptEditor(node, untrack(() => promptDoc), (text) => { promptDoc = text; }, editorModelInfo);
      if (disposed) v?.destroy?.(); else view = v;
    })();
    return { destroy() { disposed = true; view?.destroy?.(); } };
  }

  // Mount the detached poser once per open; never re-mount on recipe change (that
  // would discard the user's pose) — output mode is switched in place below.
  $effect(() => {
    if (!open || !poserEl || !onMountPoser) return;
    let disposed = false, handle = null;
    const w = untrack(() => width) || 832;
    const h = untrack(() => height) || 1216;
    const mode = untrack(() => recipe?.poserMode) || "default";
    Promise.resolve(onMountPoser(poserEl, { width: w, height: h, outputMode: mode }))
      .then((hd) => { if (disposed) hd?.dispose?.(); else { handle = hd; poserHandle = hd; } })
      .catch((err) => console.error("[Re-pose] poser mount failed", err));
    return () => { disposed = true; handle?.dispose?.(); if (poserHandle === handle) poserHandle = null; };
  });

  // Recipe switch → flip the poser's control-map mode (clay vs depth) in place.
  $effect(() => {
    const mode = recipe?.poserMode;
    if (poserHandle && mode) untrack(() => poserHandle.setOutputMode?.(mode));
  });

  const running = $derived(progress && ["building", "queueing", "running"].includes(progress.phase));
  const canRun = $derived(!!recipe?.ok && !!modelFilename && !running);
  const progressPct = $derived(
    progress?.max ? Math.min(100, Math.round((progress.value / progress.max) * 100))
      : progress?.phase === "done" ? 100 : 0);

  async function run() {
    if (!canRun || !onRun) return;
    // Force a capture in THIS recipe's mode right now (depth for RefControl, white
    // for AnyPose) — never rely on whatever mode the live viewport was left in.
    const cm = (await poserHandle?.captureNow?.(recipe.poserMode)) || poserHandle?.getControlMap?.() || { filename: "" };
    if (!cm.filename) { console.warn("[Re-pose] no control map yet"); return; }
    onRun({
      recipe,                       // caps entry: lora/clip/vae/templateId
      modelFilename,
      promptDoc,                    // raw PromptChain doc — compiled server-side (single source of truth)
      loraStrength,                 // pose-LoRA weight override
      megapixels: recipe.megapixels ? megapixels : null, // Qwen input scale (AnyPose only)
      sampler: {
        seed: randomizeSeed ? 0 : seed,  // 0 → runner randomizes
        steps, cfg,
        sampler: recipe.sampler?.sampler || "euler",
        scheduler: recipe.sampler?.scheduler || "simple",
        denoise: recipe.sampler?.denoise ?? 1.0,
      },
      controlMapFilename: cm.filename,
    });
  }

  function progressText(p) {
    if (!p) return "";
    if (p.phase === "building") return "Building graph…";
    if (p.phase === "queueing") return "Queueing…";
    if (p.phase === "running") return p.max ? `Rendering… ${p.value}/${p.max} (${progressPct}%)` : "Rendering…";
    if (p.phase === "done") return "Done.";
    if (p.phase === "error") return `Error: ${p.message || "failed"}`;
    if (p.phase === "cancelled") return "Cancelled.";
    return "";
  }
</script>

{#if open}
  <!-- No outside-click close: a text-selection drag that releases on the backdrop
       must not nuke the modal. Close via the ✕ or Cancel/Close button. -->
  <div use:portal class="pcr-modal-backdrop" style:z-index={10006}>
    <div class="pcr-modal pcr-rp-modal" role="dialog" aria-modal="true" aria-label="Re-pose">
      <div class="pcr-modal-header">
        <span class="pcr-modal-title">{running ? "Re-posing…" : progress?.phase === "done" ? "Re-pose Complete" : "Re-pose"}</span>
        <button class="pcr-modal-close" onclick={() => !running && onCancel()} aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>

      <div class="pcr-modal-body pcr-rp-body">
        <!-- LEFT: detached 3D poser -->
        <div class="pcr-rp-stage">
          <div class="pcr-rp-stage-label">Pose</div>
          <div class="pcr-rp-poser-mount" bind:this={poserEl}></div>
        </div>

        <!-- MIDDLE: source, then live preview / result during a run -->
        <div class="pcr-rp-stage">
          <div class="pcr-rp-stage-label">{running ? "Rendering" : progress?.phase === "done" ? "Result" : "Source"}</div>
          <div class="pcr-rp-stage-img">
            {#if (running || progress?.phase === "done") && (progress?.resultUrl || progress?.previewUrl)}
              <img src={progress.resultUrl || progress.previewUrl} alt="preview" draggable="false" />
            {:else if sourceUrl}
              <img src={sourceUrl} alt="source" draggable="false" />
            {/if}
          </div>
          {#if progress && progress.phase !== "building"}
            <div class="pcr-rp-bar-wrap">
              <div class="pcr-rp-bar"><div class="pcr-rp-bar-fill" style:width={progressPct + "%"}></div></div>
              <span class="pcr-rp-bar-text" class:err={progress.phase === "error"}>{progressText(progress)}</span>
            </div>
          {/if}
        </div>

        <!-- RIGHT: recipe / model / prompt / settings -->
        <div class="pcr-rp-config" class:running={running}>
          <div class="pcr-mcard">
            <div class="pcr-mcard-title">Recipe</div>
            <select class="pcr-rp-select" value={selectedRecipeId} onchange={onRecipeChange}>
              {#each recipes as r}
                <option value={r.id} disabled={!r.ok}>{r.label}{r.ok ? "" : ` — ${r.reason}`}</option>
              {/each}
            </select>
            {#if recipe}<div class="pcr-rp-hint">{recipe.blurb}</div>{/if}
          </div>

          <div class="pcr-mcard">
            <div class="pcr-mcard-title">Base model</div>
            <select class="pcr-rp-select" bind:value={modelFilename} disabled={!recipe?.models?.length}>
              {#each recipe?.models || [] as m}
                <option value={m.filename}>{m.displayName}</option>
              {/each}
            </select>
          </div>

          <div class="pcr-mcard">
            <div class="pcr-mcard-title">Prompt</div>
            {#key selectedRecipeId}
              {#if mountPromptEditor}
                <div class="pcr-rp-text pcr-rp-editor" use:promptEditor></div>
              {:else}
                <textarea class="pcr-rp-text" rows="9" bind:value={promptDoc} spellcheck="false"></textarea>
              {/if}
            {/key}
          </div>

          <div class="pcr-mcard">
            <div class="pcr-mcard-title">Settings</div>
            <div class="pcr-rp-row">
              <label class="pcr-rp-field"><span>Steps</span><input type="number" min="1" max="100" bind:value={steps} /></label>
              <label class="pcr-rp-field"><span>CFG</span><input type="number" min="1" max="20" step="0.5" bind:value={cfg} /></label>
            </div>
            <div class="pcr-rp-row">
              <label class="pcr-rp-field"><span>Pose LoRA strength</span><input type="number" min="0" max="2" step="0.05" bind:value={loraStrength} /></label>
              {#if recipe?.megapixels}
                <label class="pcr-rp-field"><span>Input scale (MP)</span><input type="number" min="0.25" max="4" step="0.25" bind:value={megapixels} /></label>
              {/if}
            </div>
            <label class="pcr-rp-check"><input type="checkbox" bind:checked={randomizeSeed} /> Randomize seed</label>
            {#if !randomizeSeed}
              <label class="pcr-rp-field"><span>Seed</span><input type="number" bind:value={seed} /></label>
            {/if}
            {#if recipe?.poserMode === "depth"}
              <div class="pcr-rp-hint">Depth-locked: the poser outputs a depth map; output follows the pose frame.</div>
            {/if}
          </div>
        </div>
      </div>

      <div class="pcr-modal-footer">
        {#if running}
          <button class="pcr-modal-btn pcr-modal-btn-danger" onclick={() => onCancel()}>Cancel</button>
        {:else if progress?.phase === "done"}
          <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={() => onCancel()}>Close</button>
          <button class="pcr-modal-btn pcr-modal-btn-primary" disabled={!onUseInEdit || !progress.resultUrl} onclick={() => onUseInEdit?.(progress)}>Add to Edit</button>
        {:else if progress?.phase === "error"}
          <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={() => onCancel()}>Close</button>
          <button class="pcr-modal-btn pcr-modal-btn-primary" disabled={!canRun} onclick={run}>Retry</button>
        {:else}
          <button class="pcr-modal-btn pcr-modal-btn-secondary" onclick={() => onCancel()}>Cancel</button>
          <button class="pcr-modal-btn pcr-modal-btn-primary" disabled={!canRun} onclick={run}>Run</button>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  /* Same sizing as the upscale/inpaint modals (modal-shared.css gives the chrome). */
  .pcr-rp-modal {
    width: 96vw; height: 94vh;
    min-width: 900px; max-width: 96vw;
    display: flex; flex-direction: column;
  }
  .pcr-rp-body { display: flex; gap: 16px; flex: 1; min-height: 0; }
  .pcr-rp-stage {
    flex: 1; min-width: 0; min-height: 0;
    display: flex; flex-direction: column; gap: 8px;
  }
  .pcr-rp-stage-label { font-size: 10.5px; font-weight: 700; letter-spacing: 0.7px; text-transform: uppercase; color: #7c7c7c; }
  .pcr-rp-poser-mount {
    flex: 1; min-height: 0;
    background: #101010; border: 1px solid #2a2a2a; border-radius: 6px; overflow: hidden;
  }
  .pcr-rp-stage-img {
    flex: 1; min-height: 0;
    display: flex; align-items: center; justify-content: center;
    background: #101010; border: 1px solid #2a2a2a; border-radius: 6px; overflow: hidden;
  }
  .pcr-rp-stage-img img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .pcr-rp-bar-wrap { display: flex; flex-direction: column; gap: 5px; }
  .pcr-rp-bar { height: 6px; border-radius: 3px; background: #2c2c33; overflow: hidden; }
  .pcr-rp-bar-fill { height: 100%; background: #c85909; transition: width 0.15s linear; }
  .pcr-rp-bar-text { font-size: 11px; color: #c9a87d; }
  .pcr-rp-bar-text.err { color: #e07a7a; }

  .pcr-rp-config { flex: 0 0 360px; min-height: 0; overflow-y: auto; padding-right: 4px; }
  .pcr-rp-config.running { pointer-events: none; opacity: 0.55; }
  .pcr-rp-select, .pcr-rp-text {
    width: 100%; box-sizing: border-box;
    background: #0f0f12; border: 1px solid #3a3a3a; border-radius: 6px;
    color: #e6e6e6; padding: 7px 9px; font-size: 13px;
  }
  .pcr-rp-text { resize: vertical; font-family: inherit; line-height: 1.4; }
  /* Rich editor mount — same lifecycle/sizing as the upscale modal's. */
  .pcr-rp-editor { height: 210px; padding: 0; resize: none; overflow: hidden; display: flex; flex-direction: column; }
  .pcr-rp-editor:focus-within { border-color: #c85909; }
  .pcr-rp-editor :global(.cm-editor) { flex: 1; min-height: 0; height: 100%; }
  .pcr-rp-editor :global(.cm-scroller) { overflow: auto; }
  .pcr-rp-hint { font-size: 11.5px; color: #8a8a92; line-height: 1.4; }
  .pcr-rp-row { display: flex; gap: 8px; }
  .pcr-rp-field { flex: 1; display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  .pcr-rp-field > span { color: #b6b6be; }
  .pcr-rp-field input {
    background: #0f0f12; border: 1px solid #3a3a3a; border-radius: 6px;
    color: #e6e6e6; padding: 6px 8px; font-size: 13px; width: 100%; box-sizing: border-box;
  }
  .pcr-rp-check { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #b6b6be; }
</style>
