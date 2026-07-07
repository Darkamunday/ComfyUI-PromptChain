<script>
  import { untrack } from "svelte";
  import { api } from "/scripts/api.js";
  import { safeJson, HttpError } from "../../lib/api-context.js";

  let {
    civitaiResult,
    onClose,
    onModelReady,
    onBeforeRestart,
  } = $props();

  const { model_name, architecture, family, civitai_model_id, file } = civitaiResult;
  const civitaiUrl = `https://civitai.com/models/${civitai_model_id}`;

  function inferDownloadFolder(result) {
    const f = result?.file || {};
    const haystack = [
      f.filename,
      f.name,
      result?.model_name,
      result?.version_name,
      result?.base_model,
      result?.civitai_model_id,
      result?.civitai_version_id,
    ].filter(Boolean).join(" ").toLowerCase();

    if (haystack.includes("zitremix") || haystack.includes("zit - remix") || result?.civitai_model_id === 2304785 || result?.civitai_version_id === 2642834) {
      return "diffusion_models";
    }
    if ((haystack.includes("zimage") || haystack.includes("z-image")) && !haystack.includes("aio")) {
      return "diffusion_models";
    }
    return f.folder || "checkpoints";
  }

  file.folder = inferDownloadFolder(civitaiResult);

  let downloading = $state(false);
  let progress = $state(0);
  let downloadedMB = $state(0);
  let totalMB = $state(0);
  let statusText = $state("");
  let apiKey = $state("");
  let showApiKey = $state(false);
  let hasApiKey = $state(true);
  let fileDetected = $state(false);
  let restarting = $state(false);

  let showRestart = $derived(fileDetected && !restarting);
  let restartAc = null;

  $effect(() => () => { restartAc?.abort(); });

  $effect(() => {
    untrack(() => {
      fetch("/promptchain/civitai/api-key")
        .then(r => r.json())
        .then(data => { hasApiKey = data.has_key; })
        .catch(() => {});
    });
  });

  $effect(() => {
    function onProgress({ detail }) {
      if (detail.filename !== file.filename) return;
      downloading = true;
      progress = detail.progress;
      downloadedMB = Math.round(detail.downloaded / 1048576);
      totalMB = Math.round(detail.total / 1048576);
      statusText = "Downloading\u2026";
    }

    function onDone({ detail }) {
      if (detail.filename !== file.filename) return;
      if (detail.status === "completed") {
        progress = 100;
        fileDetected = true;
        statusText = "\u2714 Download complete!";
      } else {
        statusText = `Failed: ${detail.error || "unknown error"}`;
        downloading = false;
        if (detail.error?.includes("401") || detail.error?.includes("unauthorized")) {
          showApiKey = true;
        }
      }
    }

    api.addEventListener("promptchain_download_progress", onProgress);
    api.addEventListener("promptchain_download_done", onDone);

    return () => {
      api.removeEventListener("promptchain_download_progress", onProgress);
      api.removeEventListener("promptchain_download_done", onDone);
    };
  });

  function handleOverlayClick(e) {
    if (e.target === e.currentTarget) onClose();
  }

  async function startDownload() {
    downloading = true;
    progress = 0;
    statusText = "Starting download\u2026";

    try {
      const res = await fetch("/promptchain/civitai/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: file.download_url,
          filename: file.filename,
          folder: file.folder,
          api_key: apiKey || undefined,
          // Passed through so the backend invalidates the versions
          // cache on successful download — stops the just-installed
          // version from re-appearing as a download bubble.
          civitai_model_id,
        }),
      });
      const data = await safeJson(res);
      if (data.error) {
        statusText = data.error;
        downloading = false;
      }
    } catch (err) {
      if (err instanceof HttpError) {
        const body = err.body ? (() => { try { return JSON.parse(err.body); } catch { return null; } })() : null;
        statusText = body?.error || `Server error: ${err.status} ${err.statusText}`;
      } else {
        console.error("[PromptChain] download start failed:", err);
        statusText = err.message || "Network error";
      }
      downloading = false;
    }
  }

  async function saveApiKey() {
    const key = apiKey.trim();
    if (!key) return;
    await fetch("/promptchain/civitai/api-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    hasApiKey = true;
    showApiKey = false;
    statusText = "Key saved.";
  }

  // Resolve the folder key to an absolute path so the user sees the
  // real destination instead of just "checkpoints".
  let folderPath = $state("");
  $effect(() => {
    untrack(() => {
      if (!file?.folder) return;
      fetch(`/promptchain/system/folder-path?folder=${encodeURIComponent(file.folder)}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => { folderPath = data?.path || ""; })
        .catch(() => {});
    });
  });

  function openFolder() {
    fetch("/promptchain/system/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: file?.folder || "checkpoints" }),
    });
  }

  async function restartAndResolve() {
    // Fire the pre-restart hook synchronously so any node-level work
    // (e.g. swapping a ckpt widget to the freshly-downloaded filename)
    // happens atomically with the user's decision to restart.  If the
    // user dismisses the modal instead of clicking Restart, the hook
    // never fires and the workflow stays on the old model.
    try { onBeforeRestart?.(file.filename); } catch (e) {
      console.error("[PromptChain] onBeforeRestart failed:", e);
    }
    restarting = true;
    statusText = "Restarting server\u2026";
    restartAc = new AbortController();
    const { signal } = restartAc;

    fetch("/promptchain/system/restart", { method: "POST" }).catch(() => {});

    // Per-probe timeout: the server can accept a TCP connection during
    // restart but never respond, which with no per-fetch cap would pin
    // each loop iteration indefinitely.
    const probe = async (url, timeoutMs) => {
      const outer = new AbortController();
      const timer = setTimeout(() => outer.abort(), timeoutMs);
      const cancelListener = () => outer.abort();
      signal.addEventListener("abort", cancelListener);
      try {
        return await fetch(url, { signal: outer.signal });
      } finally {
        clearTimeout(timer);
        signal.removeEventListener("abort", cancelListener);
      }
    };

    for (let i = 0; i < 120; i++) {
      if (signal.aborted) return;
      await new Promise(r => setTimeout(r, 500));
      try {
        const r = await probe("/api/system_stats", 1500);
        if (r.ok) break;
      } catch { if (signal.aborted) return; }
    }

    statusText = "Scanning model\u2026";

    for (let i = 0; i < 20; i++) {
      if (signal.aborted) return;
      try {
        const r = await probe(`/promptchain/models/identity?file=${encodeURIComponent(file.filename)}`, 2000);
        if (r.ok) {
          onModelReady(file.filename);
          return;
        }
      } catch { if (signal.aborted) return; }
      await new Promise(r => setTimeout(r, 1000));
    }

    statusText = "Model not detected after restart.";
    restarting = false;
  }

  function cancel() {
    if (downloading && !fileDetected) {
      fetch("/promptchain/civitai/download-cancel", { method: "POST" }).catch(() => {});
    }
    onClose();
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="pcr-download-overlay" onclick={handleOverlayClick}>
  <div class="pcr-download-modal">
    <div class="pcr-download-header">
      <div class="pcr-download-title">Get Model: {model_name}</div>
      <div class="pcr-download-meta">
        {architecture} &middot; {family} &middot; {file.size_gb} GB &middot;
        <a href={civitaiUrl} target="_blank" rel="noopener">CivitAI</a>
      </div>
    </div>

    <div class="pcr-download-body">
      <div class="pcr-download-section">
        <div class="pcr-download-section-label">Download from CivitAI:</div>
        <a href={file.download_url} target="_blank" rel="noopener" class="pcr-download-link">{file.download_url}</a>
      </div>

      <div class="pcr-download-section">
        <div class="pcr-download-section-label">Expected filename:</div>
        <code class="pcr-download-filename">{file.filename}</code>
      </div>

      <div class="pcr-download-section">
        <div class="pcr-download-section-label">Destination folder ({file.folder}):</div>
        <div class="pcr-download-folder-row">
          <code class="pcr-download-folder-path" title={folderPath || file.folder}>{folderPath || file.folder}</code>
          <button class="pcr-download-btn" onclick={openFolder}>Open Folder</button>
        </div>
      </div>
    </div>

    {#if downloading || progress > 0}
      <div class="pcr-download-progress-wrap">
        <div class="pcr-download-progress-bar">
          <div class="pcr-download-progress-fill" style="width: {progress}%"></div>
        </div>
        <div class="pcr-download-progress-text">
          {downloadedMB} / {totalMB} MB ({Math.round(progress)}%)
        </div>
      </div>
    {/if}

    {#if !hasApiKey || showApiKey}
      <div class="pcr-download-key-wrap">
        <div class="pcr-download-key-label">
          API key for auto-download &middot;
          <a href="https://civitai.com/user/account" target="_blank" rel="noopener" class="pcr-download-key-link">get key</a>
        </div>
        <input
          type="password"
          class="pcr-picker-search"
          placeholder="Paste API key..."
          bind:value={apiKey}
        />
        <button class="pcr-download-btn pcr-download-btn-primary" onclick={saveApiKey}>Save Key</button>
      </div>
    {/if}

    <div class="pcr-download-footer">
      <div class="pcr-download-status">{statusText}</div>
      <div class="pcr-download-buttons">
        {#if showRestart}
          <button class="pcr-download-btn pcr-download-btn-primary" onclick={restartAndResolve}>
            Restart ComfyUI
          </button>
        {:else if !restarting}
          <button
            class="pcr-download-btn pcr-download-btn-primary"
            onclick={startDownload}
            disabled={downloading || (!hasApiKey && !apiKey.trim())}
          >
            {downloading ? "Downloading\u2026" : "Auto Download"}
          </button>
        {/if}
        {#if restarting}
          <button class="pcr-download-btn" disabled>Restarting\u2026</button>
        {:else}
          <button class="pcr-download-btn" onclick={cancel}>Cancel</button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .pcr-download-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(3px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 999999;
  }
  .pcr-download-modal {
    background: #1e1e1e;
    border: 1px solid #444;
    border-radius: 8px;
    padding: 20px 24px;
    min-width: 360px;
    max-width: 460px;
  }
  .pcr-download-title {
    font-size: 14px;
    font-weight: 600;
    color: #eee;
    margin-bottom: 4px;
  }
  .pcr-download-meta {
    font-size: 11px;
    color: #888;
    margin-bottom: 16px;
  }
  .pcr-download-progress-wrap { margin-bottom: 8px; }
  .pcr-download-progress-bar {
    height: 6px;
    background: #333;
    border-radius: 3px;
    overflow: hidden;
  }
  .pcr-download-progress-fill {
    height: 100%;
    background: #4fc3f7;
    border-radius: 3px;
    width: 0%;
    transition: width 0.3s;
  }
  .pcr-download-progress-text {
    font-size: 11px;
    color: #999;
    margin-top: 4px;
    text-align: right;
  }
  .pcr-download-status {
    font-size: 12px;
    color: #ccc;
    margin-bottom: 12px;
    min-height: 18px;
  }
  .pcr-download-buttons {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .pcr-download-btn {
    padding: 6px 16px;
    font-size: 12px;
    border: 1px solid #555;
    border-radius: 4px;
    background: transparent;
    color: #ccc;
    cursor: pointer;
  }
  .pcr-download-btn:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-download-btn:disabled { opacity: 0.5; cursor: default; }
  .pcr-download-btn-primary {
    background: #4fc3f7;
    color: #111;
    border-color: #4fc3f7;
  }
  .pcr-download-btn-primary:hover { background: #39b0e4; }
  .pcr-download-btn-primary:disabled { background: #2a7a9e; }
  .pcr-download-header { margin-bottom: 12px; }
  .pcr-download-body { margin-bottom: 12px; }
  .pcr-download-section { margin-bottom: 10px; }
  .pcr-download-section-label { font-size: 11px; color: #999; margin-bottom: 4px; }
  .pcr-download-filename {
    font-size: 12px;
    color: #ccc;
  }
  .pcr-download-label { font-size: 11px; color: #999; margin-bottom: 4px; }
  .pcr-download-link {
    color: #4fc3f7;
    font-size: 12px;
    text-decoration: none;
    word-break: break-all;
  }
  .pcr-download-link:hover { text-decoration: underline; }
  .pcr-download-folder-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .pcr-download-folder-path {
    font-size: 11px;
    color: #aaa;
    background: rgba(255,255,255,0.04);
    padding: 4px 8px;
    border-radius: 3px;
    flex: 1;
    word-break: break-all;
  }
  .pcr-download-footer { margin-top: 8px; }
  .pcr-download-key-wrap { margin-bottom: 12px; }
  .pcr-download-key-label { font-size: 11px; color: #999; }
  .pcr-download-key-link { color: #4fc3f7; font-size: 11px; }
</style>
