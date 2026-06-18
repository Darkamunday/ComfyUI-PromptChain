<script>
  // InfoTab — model metadata form (author, description, trigger words, etc.)

  import "./model-panel-shared.css";
  import ClickToEditField from "./ClickToEditField.svelte";

  let {
    modelInfo = {},
    savedConfig = null,
    architectures = [],
    families = {},
    onConfigUpdate,
  } = $props();

  let config = $state({ ...(savedConfig || {}) });
  let saveStatus = $state("");
  let saveTimer = null;

  $effect(() => () => { clearTimeout(saveTimer); });

  // Classification fields
  let currentArch = $state(config.architecture || modelInfo.architecture || "");
  let currentFamily = $state(config.family || "");
  let currentModelName = $state(config.model_name || modelInfo.filename || "");
  let currentVersion = $state(config.version || "");

  // Info fields
  let author = $state(config.author || "");
  let description = $state(config.description || "");
  let license = $state(config.license || "");
  let tags = $state((config.tags || []).join(", "));
  let url = $state(config.url || "");
  let downloadUrl = $state(config.download_url || "");
  let civitaiId = $state(config.civitai_model_id || "");
  let trigger = $state(config.trigger || "");
  let negative = $state(config.negative || "");
  let qualityPosition = $state(config.quality_position || "");
  let releaseDate = $state(config.release_date || "");
  let notes = $state(config.notes || "");


  function familiesForArch(arch) { return families[arch] || []; }

  function saveClassification() {
    const mName = currentModelName.trim() || modelInfo.filename;
    const ver = currentVersion.trim();
    const composed = ver ? `${mName} - ${ver}` : mName;
    config = {
      ...config,
      architecture: currentArch,
      family: currentFamily || undefined,
      model_name: mName,
      version: ver || undefined,
      display_name: composed,
    };
    if (!config.nodes) config.nodes = {};
    doSave(config, true);
  }

  async function doSave(cfg, isClassification = false) {
    saveStatus = "Saving...";
    try {
      await fetch(`/promptchain/models/settings/${modelInfo.hash}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      saveStatus = isClassification ? "" : "Saved";
      onConfigUpdate?.(cfg);
      if (isClassification) {
        const composed = cfg.display_name || cfg.model_name || modelInfo.filename;
        window.dispatchEvent(new CustomEvent("pcr-model-renamed", { detail: { hash: modelInfo.hash, name: composed } }));
      }
      if (!isClassification) { saveTimer = setTimeout(() => { saveStatus = ""; }, 1000); }
    } catch {
      saveStatus = "Error";
    }
  }

  async function handleSaveInfo() {
    const cfg = { ...config };
    cfg.author = author.trim() || undefined;
    cfg.description = description.trim() || undefined;
    cfg.license = license.trim() || undefined;
    const tagsVal = tags.trim();
    cfg.tags = tagsVal ? tagsVal.split(",").map(t => t.trim()).filter(Boolean) : undefined;
    cfg.url = url.trim() || undefined;
    cfg.download_url = downloadUrl.trim() || undefined;
    const cid = civitaiId.toString().trim();
    cfg.civitai_model_id = cid ? parseInt(cid, 10) || undefined : undefined;
    cfg.trigger = trigger.trim() || undefined;
    cfg.negative = negative.trim() || undefined;
    cfg.quality_position = qualityPosition.trim() || undefined;
    cfg.release_date = releaseDate.trim() || undefined;
    cfg.notes = notes.trim() || undefined;
    if (!cfg.nodes) cfg.nodes = {};
    config = cfg;
    doSave(cfg);
  }

  let archOptions = $derived.by(() => {
    const opts = [...architectures];
    if (currentArch && !architectures.some(a => a.id === currentArch)) {
      opts.push({ id: currentArch, label: currentArch });
    }
    return opts;
  });
</script>

<div class="pcr-model-panel-body">
  <div class="pcr-info-form">
    <!-- Classification -->
    <div class="pcr-info-classification">
      <div class="pcr-model-panel-field-row">
        <ClickToEditField label="Architecture" value={currentArch} type="select"
          options={archOptions}
          onChange={(val) => { currentArch = val; currentFamily = ""; saveClassification(); }} />
        <ClickToEditField label="Family" value={currentFamily} type="select"
          options={familiesForArch(currentArch)}
          onChange={(val) => { currentFamily = val; saveClassification(); }} />
      </div>
      <div class="pcr-model-panel-field-row">
        <ClickToEditField label="Model" value={currentModelName} type="text"
          onChange={(val) => { currentModelName = val; saveClassification(); }} />
        <ClickToEditField label="Version" value={currentVersion} type="text"
          onChange={(val) => { currentVersion = val; saveClassification(); }} />
      </div>
    </div>

    <!-- Info fields -->
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Author</div>
      <input type="text" bind:value={author} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Description</div>
      <textarea bind:value={description}></textarea>
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">License</div>
      <input type="text" bind:value={license} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Tags</div>
      <input type="text" bind:value={tags} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Model URL</div>
      <input type="text" bind:value={url} />
      {#if url}
        <a href={url} target="_blank" rel="noopener"
          style="font-size:11px;color:#4fc3f7;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;margin-top:2px">
          {url}
        </a>
      {/if}
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Download URL</div>
      <input type="text" bind:value={downloadUrl} />
      {#if downloadUrl}
        <a href={downloadUrl} target="_blank" rel="noopener"
          style="font-size:11px;color:#4fc3f7;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;margin-top:2px">
          {downloadUrl}
        </a>
      {/if}
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">CivitAI Model ID</div>
      <input type="text" bind:value={civitaiId} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Trigger Words</div>
      <textarea bind:value={trigger}></textarea>
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Default Negative</div>
      <textarea bind:value={negative}></textarea>
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Quality Tag Position</div>
      <input type="text" bind:value={qualityPosition} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Release Date</div>
      <input type="text" bind:value={releaseDate} />
    </div>
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Notes</div>
      <textarea bind:value={notes}></textarea>
    </div>

    <!-- Hash (read-only) -->
    <div class="pcr-info-field">
      <div class="pcr-info-field-label">Fingerprint</div>
      <div class="pcr-info-field-readonly">{modelInfo.hash}</div>
    </div>

    <button class="pcr-model-panel-save" onclick={handleSaveInfo}
      disabled={saveStatus === "Saving..."}>
      {saveStatus || "Save Info"}
    </button>
  </div>
</div>

<style>
  /* classification section */
  .pcr-info-classification { padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid #333; }
  .pcr-model-panel-field-row { display: flex; gap: 16px; }

  /* info tab form */
  .pcr-info-form { display: flex; flex-direction: column; gap: 10px; }
  .pcr-info-field { display: flex; flex-direction: column; gap: 3px; }
  .pcr-info-field-label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
  .pcr-info-field input,
  .pcr-info-field textarea {
    font-size: 12px;
    padding: 5px 8px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid #444;
    border-radius: 4px;
    color: #ddd;
    outline: none;
  }
  .pcr-info-field input:focus,
  .pcr-info-field textarea:focus { border-color: #4fc3f7; }
  .pcr-info-field textarea { resize: vertical; min-height: 60px; font-family: inherit; }
  .pcr-info-field-readonly {
    font-family: monospace;
    font-size: 11px;
    color: #555;
    user-select: all;
    padding: 5px 8px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 4px;
  }

</style>
