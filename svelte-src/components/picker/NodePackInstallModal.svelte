<script>
  // Installs the custom-node pack(s) a PromptChain injectable needs
  // (FaceDetailer/PuLID/Upscaler), then restarts, re-checks, and proceeds.
  // Status comes from /promptchain/nodepacks/status; the install runs through
  // the unified section installer (/promptchain/install/install) scoped to this
  // one injectable, so every entry point shares one install path. Also drives
  // /promptchain/system/restart.

  let { injectable, onClose, onReady } = $props();

  let label = $state(injectable);
  let repos = $state([]);        // [{url, dir, cloned}]
  let missingNodes = $state([]);
  let missingModels = $state([]);
  let present = $state(false);
  let section = $state(null);    // section id this injectable belongs to

  let phase = $state("loading"); // loading | ready | installing | installed | restarting | done | error
  let statusText = $state("Checking what's needed…");
  let logLines = $state([]);
  let errorText = $state("");
  let restartAc = null;
  let logEl;

  $effect(() => () => { restartAc?.abort(); });

  // Autoscroll the log as lines stream in.
  $effect(() => {
    logLines.length;
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  });

  $effect(() => { refreshStatus(); });

  async function refreshStatus() {
    try {
      const res = await fetch(`/promptchain/nodepacks/status?injectable=${encodeURIComponent(injectable)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      label = data.label || injectable;
      repos = data.repos || [];
      missingNodes = data.missing_nodes || [];
      missingModels = data.missing_models || [];
      present = !!data.present;
      section = data.section || null;
      if (present) {
        phase = "done";
        statusText = "All set — adding it now…";
        onReady?.();
        onClose?.();
      } else if (phase === "loading") {
        phase = "ready";
        const missingRepos = repos.filter(r => !r.cloned).length;
        if (missingRepos) {
          statusText = `${label} needs ${missingRepos} custom-node pack${missingRepos > 1 ? "s" : ""}`
            + (missingModels.length ? ` and ${missingModels.length} model file${missingModels.length > 1 ? "s" : ""}.` : ".");
        } else if (missingModels.length) {
          statusText = `${label}'s nodes are installed but ${missingModels.length} model file${missingModels.length > 1 ? "s are" : " is"} missing — download to finish.`;
        } else {
          statusText = `${label}'s files are present but its nodes aren't loaded yet — restart to finish.`;
        }
      }
    } catch (e) {
      phase = "error";
      errorText = `Couldn't check status: ${e.message}`;
    }
  }

  function log(line) {
    logLines = [...logLines, line];
  }

  async function install() {
    phase = "installing";
    statusText = "Installing…";
    logLines = [];
    errorText = "";

    try {
      // Install through the unified section installer, scoped to this one
      // injectable (members filter), so the [add] modal, the splash, and the
      // Settings page all run the identical install path. Fall back to the
      // legacy per-injectable route if the section is somehow unknown.
      const useSection = !!section;
      const res = await fetch(
        useSection ? "/promptchain/install/install" : "/promptchain/nodepacks/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(useSection ? { section, members: [injectable] } : { injectable }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => "");
        throw new Error(detail || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 2);
          if (!chunk.startsWith("data:")) continue;
          let evt;
          try { evt = JSON.parse(chunk.slice(5).trim()); } catch { continue; }
          handleEvent(evt);
        }
      }
    } catch (e) {
      phase = "error";
      errorText = `Install failed: ${e.message}`;
    }
  }

  function handleEvent(evt) {
    if (evt.error) {
      phase = "error";
      errorText = evt.error;
      log("✘ " + evt.error);
      return;
    }
    if (evt.line) {
      log(evt.line);
      return;
    }
    if (evt.stage === "download") {
      statusText = `Downloading ${evt.file}… ${evt.pct ?? 0}%`;
      return;
    }
    if (evt.stage === "prefetch") {
      statusText = `Pre-fetching ${evt.file || "extra weights"}…`;
      return;
    }
    if (evt.stage) {
      const labels = {
        clone: `Cloning ${evt.repo}…`,
        pip: `Installing requirements for ${evt.repo}…`,
        install_script: `Running ${evt.repo} install script…`,
        fix_deps: "Verifying core dependencies…",
        models: `Downloading ${evt.count} model file${evt.count > 1 ? "s" : ""}…`,
      };
      statusText = labels[evt.stage] || evt.stage;
      return;
    }
    if (evt.done) {
      phase = "installed";
      statusText = "Installed. Restart ComfyUI to load the new nodes.";
    }
  }

  async function restartAndResolve() {
    phase = "restarting";
    statusText = "Restarting server…";
    restartAc = new AbortController();
    const { signal } = restartAc;

    fetch("/promptchain/system/restart", { method: "POST" }).catch(() => {});

    // Wait for the server to go down then come back up.
    for (let i = 0; i < 180; i++) {
      if (signal.aborted) return;
      await new Promise(r => setTimeout(r, 500));
      try {
        const r = await fetch("/api/system_stats", { signal });
        if (r.ok) break;
      } catch { if (signal.aborted) return; }
    }

    statusText = "Checking the new nodes…";
    for (let i = 0; i < 20; i++) {
      if (signal.aborted) return;
      try {
        const r = await fetch(`/promptchain/nodepacks/status?injectable=${encodeURIComponent(injectable)}`, { signal });
        if (r.ok) {
          const data = await r.json();
          if (data.present) {
            phase = "done";
            statusText = "Done — adding it now…";
            onReady?.();
            onClose?.();
            return;
          }
        }
      } catch { if (signal.aborted) return; }
      await new Promise(r => setTimeout(r, 1000));
    }

    phase = "error";
    errorText = "Nodes still aren't registered after restart. Check the server log for load errors.";
  }

</script>

<div class="pcr-np-overlay">
  <div class="pcr-np-modal">
    <div class="pcr-np-header">
      <div class="pcr-np-title">Add {label}</div>
      <div class="pcr-np-meta">{label} relies on a community custom-node pack that isn't installed yet.</div>
    </div>

    <div class="pcr-np-body">
      {#each repos as repo}
        <div class="pcr-np-repo-row">
          <span class="pcr-np-repo-status" style:color={repo.cloned ? "#4caf50" : "#888"}>
            {repo.cloned ? "✔" : "○"}
          </span>
          <div class="pcr-np-repo-info">
            <div class="pcr-np-repo-name">{repo.dir}</div>
            <div class="pcr-np-repo-url">{repo.url}</div>
          </div>
        </div>
      {/each}
    </div>

    {#if logLines.length}
      <div class="pcr-np-log" bind:this={logEl}>
        {#each logLines as line}<div class="pcr-np-log-line">{line}</div>{/each}
      </div>
    {/if}

    <div class="pcr-np-footer">
      <div class="pcr-np-status" class:pcr-np-status-error={phase === "error"}>
        {phase === "error" ? errorText : statusText}
      </div>
      <div class="pcr-np-buttons">
        {#if phase === "ready"}
          {#if repos.some(r => !r.cloned) || missingModels.length}
            <button class="pcr-np-btn pcr-np-btn-primary" onclick={install}>Install</button>
          {:else}
            <button class="pcr-np-btn pcr-np-btn-primary" onclick={restartAndResolve}>Restart ComfyUI</button>
          {/if}
        {:else if phase === "installing"}
          <button class="pcr-np-btn" disabled>Installing&hellip;</button>
        {:else if phase === "installed"}
          <button class="pcr-np-btn pcr-np-btn-primary" onclick={restartAndResolve}>Restart ComfyUI</button>
        {:else if phase === "restarting"}
          <button class="pcr-np-btn" disabled>Restarting&hellip;</button>
        {:else if phase === "error"}
          <button class="pcr-np-btn pcr-np-btn-primary" onclick={install}>Retry</button>
        {/if}
        {#if phase !== "installing" && phase !== "restarting"}
          <button class="pcr-np-btn" onclick={() => onClose?.()}>
            {phase === "installed" ? "Later" : "Cancel"}
          </button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .pcr-np-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(3px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 999999;
  }
  .pcr-np-modal {
    background: #1e1e1e;
    border: 1px solid #444;
    border-radius: 8px;
    padding: 20px 24px;
    min-width: 380px;
    max-width: 480px;
  }
  .pcr-np-header { margin-bottom: 12px; }
  .pcr-np-title { font-size: 14px; font-weight: 600; color: #eee; margin-bottom: 4px; }
  .pcr-np-meta { font-size: 11px; color: #888; }
  .pcr-np-body { margin-bottom: 12px; }
  .pcr-np-repo-row {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid #333;
  }
  .pcr-np-repo-row:last-child { border-bottom: none; }
  .pcr-np-repo-status {
    flex-shrink: 0;
    width: 16px;
    text-align: center;
    font-size: 14px;
    line-height: 18px;
  }
  .pcr-np-repo-info { flex: 1; min-width: 0; }
  .pcr-np-repo-name { font-size: 12px; font-weight: 500; color: #ddd; }
  .pcr-np-repo-url {
    font-size: 10px;
    color: #888;
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pcr-np-log {
    background: #111;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 8px 10px;
    margin-bottom: 12px;
    max-height: 160px;
    overflow-y: auto;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    line-height: 1.4;
    color: #9bb;
  }
  .pcr-np-log-line { white-space: pre-wrap; word-break: break-all; }
  .pcr-np-status {
    font-size: 12px;
    color: #ccc;
    margin-bottom: 12px;
    min-height: 18px;
  }
  .pcr-np-status-error { color: #f44336; }
  .pcr-np-buttons { display: flex; gap: 8px; justify-content: flex-end; }
  .pcr-np-btn {
    padding: 6px 16px;
    font-size: 12px;
    border: 1px solid #555;
    border-radius: 4px;
    background: transparent;
    color: #ccc;
    cursor: pointer;
  }
  .pcr-np-btn:hover { background: rgba(255, 255, 255, 0.08); }
  .pcr-np-btn:disabled { opacity: 0.5; cursor: default; }
  .pcr-np-btn-primary { background: #4fc3f7; color: #111; border-color: #4fc3f7; }
  .pcr-np-btn-primary:hover { background: #39b0e4; }
</style>
