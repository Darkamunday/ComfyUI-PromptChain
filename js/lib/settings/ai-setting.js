// Custom render for the PromptChain.AI setting row. Three top-level
// options: None / Cloud / Local. Cloud drills down into a service
// picker (Claude, OpenAI, Grok, Gemini, OpenRouter, DeepSeek, Groq,
// Mistral, Other). Local is Ollama / llama.cpp / LM Studio / etc.
//
// API keys are posted once and stored server-side — the browser never
// receives them back (responses carry has_key flags instead).

const CLOUD_SERVICES = [
  {
    id: "claude",
    label: "Claude (Anthropic)",
    help: "https://console.anthropic.com/settings/keys",
    keyPlaceholder: "sk-ant-...",
    models: [
      { id: "claude-haiku-4-5",  label: "Haiku 4.5 (fast, cheap)" },
      { id: "claude-sonnet-4-6", label: "Sonnet 4.6 (balanced)" },
      { id: "claude-opus-4-7",   label: "Opus 4.7 (best)" },
    ],
  },
  {
    id: "openai",
    label: "OpenAI",
    help: "https://platform.openai.com/api-keys",
    keyPlaceholder: "sk-proj-...",
    detectable: true,
    modelPlaceholder: "gpt-5 / gpt-5-mini / gpt-4o / ...",
  },
  {
    id: "grok",
    label: "xAI (Grok)",
    help: "https://console.x.ai/",
    keyPlaceholder: "xai-...",
    models: [
      { id: "grok-4",            label: "Grok 4" },
      { id: "grok-4-fast",       label: "Grok 4 Fast" },
      { id: "grok-code-fast-1",  label: "Grok Code Fast 1" },
    ],
    detectable: true,
  },
  {
    id: "gemini",
    label: "Google Gemini",
    help: "https://aistudio.google.com/app/apikey",
    keyPlaceholder: "AIza...",
    models: [
      { id: "gemini-2.5-pro",        label: "Gemini 2.5 Pro" },
      { id: "gemini-2.5-flash",      label: "Gemini 2.5 Flash" },
      { id: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite" },
    ],
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    help: "https://openrouter.ai/settings/keys",
    keyPlaceholder: "sk-or-v1-...",
    detectable: true,
    modelPlaceholder: "anthropic/claude-sonnet-4-5 / openai/gpt-5 / ...",
    hint: "One key → Claude, GPT, Grok, Gemini, Llama, and many more.",
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    help: "https://platform.deepseek.com/api_keys",
    keyPlaceholder: "sk-...",
    models: [
      { id: "deepseek-chat",     label: "DeepSeek Chat" },
      { id: "deepseek-reasoner", label: "DeepSeek Reasoner" },
    ],
  },
  {
    id: "groq",
    label: "Groq (fast inference)",
    help: "https://console.groq.com/keys",
    keyPlaceholder: "gsk_...",
    detectable: true,
    modelPlaceholder: "llama-3.3-70b-versatile / ...",
  },
  {
    id: "mistral",
    label: "Mistral",
    help: "https://console.mistral.ai/api-keys/",
    keyPlaceholder: "...",
    detectable: true,
    modelPlaceholder: "mistral-large-latest / ...",
  },
  {
    id: "other",
    label: "Other OpenAI-compatible",
    help: null,
    keyPlaceholder: "(if your provider needs one)",
    detectable: true,
    requiresBaseUrl: true,
    modelPlaceholder: "model name",
  },
];

function serviceById(id) {
  return CLOUD_SERVICES.find(s => s.id === id) || CLOUD_SERVICES[0];
}

const INPUT_STYLE = "padding:6px 10px;background:var(--comfy-input-bg, #2a2a2a);border:1px solid var(--border-color, #555);border-radius:4px;color:var(--input-text, #ddd);font-size:13px;outline:none;box-sizing:border-box;";
const BTN_STYLE = "padding:5px 14px;border:none;border-radius:4px;background:var(--comfy-input-bg, #333);color:var(--input-text, #ddd);font-size:12px;cursor:pointer;white-space:nowrap;";

export function renderAiSetting() {
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex;flex-direction:column;gap:10px;width:100%;";

  // Provider radios — immediately interactive. Only the Local block does
  // Ollama detection, and it shows its own "checking" state internally;
  // None/Cloud don't need detection and must never wait on it.
  const radioRow = document.createElement("div");
  radioRow.style.cssText = "display:flex;gap:16px;flex-wrap:wrap;";
  const radios = {};
  for (const [val, label] of [[null, "None"], ["cloud", "Cloud"], ["local", "Local (Ollama / llama.cpp / LM Studio)"]]) {
    const lbl = document.createElement("label");
    lbl.style.cssText = "display:flex;align-items:center;gap:6px;cursor:pointer;";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "pcr-ai-provider";
    input.value = val ?? "";
    const span = document.createElement("span");
    span.textContent = label;
    span.style.cssText = "font-size:13px;color:var(--input-text, #ddd);";
    lbl.append(input, span);
    radioRow.append(lbl);
    radios[val ?? "none"] = input;
  }
  wrap.append(radioRow);

  const cloudBlock = buildCloudBlock();
  wrap.append(cloudBlock.el);

  // When the guided setup finishes downloading the recommended model, flip
  // the provider to Local, select that model, and persist — so a user who
  // came in with nothing is fully configured without touching the form.
  const localBlock = buildLocalBlock((modelName) => {
    setProviderUI("local");
    localBlock.modelInput.value = modelName;
    save({ provider: "local" });
  });
  wrap.append(localBlock.el);

  const actions = document.createElement("div");
  actions.style.cssText = "display:flex;align-items:center;gap:10px;";
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save";
  saveBtn.style.cssText = BTN_STYLE + "background:#0e639c;color:#fff;";
  const testBtn = document.createElement("button");
  testBtn.textContent = "Test connection";
  testBtn.style.cssText = BTN_STYLE;
  const status = document.createElement("span");
  status.style.cssText = "font-size:12px;color:#888;";
  actions.append(saveBtn, testBtn, status);
  wrap.append(actions);

  let currentProvider = null;
  let cloudHasKey = false;

  function updateVisibility() {
    cloudBlock.el.style.display = currentProvider === "cloud" ? "" : "none";
    localBlock.el.style.display = currentProvider === "local" ? "" : "none";
    actions.style.display = currentProvider ? "" : "none";
  }

  function setProviderUI(p) {
    currentProvider = p;
    radios[p ?? "none"].checked = true;
    updateVisibility();
  }

  for (const input of Object.values(radios)) {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      currentProvider = input.value || null;
      updateVisibility();
      save({ provider: currentProvider });
    });
  }

  async function loadConfig() {
    try {
      let cfg = await (await fetch("/promptchain/ai/config")).json();
      // First-run: default to Local if Ollama already serves the recommended
      // model, then re-read so the UI reflects it.
      if (!cfg.provider) {
        try {
          const auto = await (await fetch("/promptchain/ai/auto-configure", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          })).json();
          if (auto.configured) cfg = await (await fetch("/promptchain/ai/config")).json();
        } catch {}
      }
      setProviderUI(cfg.provider || null);

      const cloudService = cfg.cloud?.service || "claude";
      cloudHasKey = !!cfg.cloud?.has_key;
      cloudBlock.applyConfig({
        service: cloudService,
        model: cfg.cloud?.model || "",
        baseUrl: cfg.cloud?.base_url || "",
        hasKey: cloudHasKey,
      });

      localBlock.urlInput.value = cfg.local?.base_url || "http://localhost:11434/v1";
      localBlock.modelInput.value = cfg.local?.model || "";
      localBlock.autoStartInput.checked = cfg.local?.auto_start !== false;
    } catch {
      status.textContent = "✗ could not load config";
      status.style.color = "#e55";
    }
  }

  function buildPayload(overrides = {}) {
    const body = { provider: "provider" in overrides ? overrides.provider : currentProvider };
    const activeProvider = body.provider;
    if (activeProvider === "cloud" || currentProvider === "cloud") {
      const cloudState = cloudBlock.readState();
      const cloud = {
        service: cloudState.service,
        model: cloudState.model,
      };
      if (cloudState.key) cloud.api_key = cloudState.key;
      if (cloudState.baseUrl) cloud.base_url = cloudState.baseUrl;
      body.cloud = cloud;
    }
    if (activeProvider === "local" || currentProvider === "local") {
      body.local = {
        base_url: localBlock.urlInput.value.trim(),
        model: localBlock.modelInput.value.trim(),
        auto_start: localBlock.autoStartInput.checked,
      };
    }
    return body;
  }

  async function save(overrides = {}) {
    const body = buildPayload(overrides);
    try {
      const r = await fetch("/promptchain/ai/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const cfg = await r.json();
      cloudHasKey = !!cfg.cloud?.has_key;
      cloudBlock.clearKey(cloudHasKey);
      // Let already-mounted nodes re-attempt their auto-open now that a
      // provider exists (e.g. user configured AI during onboarding).
      if (cfg.provider) window.dispatchEvent(new CustomEvent("promptchain:ai-configured"));
      status.textContent = "✓ saved";
      status.style.color = "#4ec96b";
      setTimeout(() => {
        if (status.textContent === "✓ saved") status.textContent = "";
      }, 1500);
    } catch {
      status.textContent = "✗ save failed";
      status.style.color = "#e55";
    }
  }

  async function test() {
    status.textContent = "testing...";
    status.style.color = "#888";
    const body = buildPayload();
    try {
      const r = await fetch("/promptchain/ai/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (data.ok) {
        status.textContent = "✓ connected";
        status.style.color = "#4ec96b";
      } else {
        status.textContent = "✗ " + (data.error || "failed");
        status.style.color = "#e55";
      }
    } catch (e) {
      status.textContent = "✗ " + (e.message || "failed");
      status.style.color = "#e55";
    }
  }

  saveBtn.addEventListener("click", () => save());
  testBtn.addEventListener("click", test);

  loadConfig();
  return wrap;
}

// ── cloud block ─────────────────────────────────────────────────────

function buildCloudBlock() {
  const el = document.createElement("div");
  el.style.cssText = "display:flex;flex-direction:column;gap:8px;padding:10px 12px;background:rgba(255,255,255,0.03);border-radius:4px;";

  // Service picker.
  const svcRow = rowWithLabel("Service");
  const svcSelect = document.createElement("select");
  svcSelect.style.cssText = INPUT_STYLE + "flex:1;";
  for (const s of CLOUD_SERVICES) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.label;
    svcSelect.append(opt);
  }
  svcRow.row.append(svcSelect);
  el.append(svcRow.row);

  // Base URL row (only visible for "other").
  const baseRow = rowWithLabel("Base URL");
  const baseInput = document.createElement("input");
  baseInput.type = "text";
  baseInput.spellcheck = false;
  baseInput.autocomplete = "off";
  baseInput.placeholder = "https://api.example.com/v1";
  baseInput.style.cssText = INPUT_STYLE + "flex:1;font-family:monospace;";
  baseRow.row.append(baseInput);
  el.append(baseRow.row);

  // API key.
  const keyRow = rowWithLabel("API key");
  // type=text + CSS masking rather than type=password — avoids the browser's
  // "save password?" prompt firing on these API-key fields.
  const keyInput = document.createElement("input");
  keyInput.type = "text";
  keyInput.spellcheck = false;
  keyInput.autocomplete = "off";
  keyInput.style.cssText = INPUT_STYLE + "flex:1;font-family:monospace;-webkit-text-security:disc;";
  const keyHint = document.createElement("a");
  keyHint.target = "_blank";
  keyHint.textContent = "get key";
  keyHint.style.cssText = "font-size:12px;color:#5b9bd5;";
  keyRow.row.append(keyInput, keyHint);
  el.append(keyRow.row);

  // Model.
  const modelRow = rowWithLabel("Model");
  const modelSelect = document.createElement("select");
  modelSelect.style.cssText = INPUT_STYLE + "flex:1;";
  const modelInput = document.createElement("input");
  modelInput.type = "text";
  modelInput.spellcheck = false;
  modelInput.autocomplete = "off";
  modelInput.setAttribute("list", "pcr-ai-cloud-models");
  modelInput.style.cssText = INPUT_STYLE + "flex:1;font-family:monospace;";
  const modelsDatalist = document.createElement("datalist");
  modelsDatalist.id = "pcr-ai-cloud-models";
  const detectBtn = document.createElement("button");
  detectBtn.textContent = "Detect";
  detectBtn.style.cssText = BTN_STYLE;
  modelRow.row.append(modelSelect, modelInput, modelsDatalist, detectBtn);
  el.append(modelRow.row);

  const detectStatus = document.createElement("div");
  detectStatus.style.cssText = "font-size:11px;color:#888;padding-left:78px;";
  el.append(detectStatus);

  const hintEl = document.createElement("div");
  hintEl.style.cssText = "font-size:11px;color:#888;padding-left:78px;";
  el.append(hintEl);

  function applyService(serviceId) {
    const svc = serviceById(serviceId);
    keyInput.placeholder = svc.keyPlaceholder || "";
    if (svc.help) {
      keyHint.href = svc.help;
      keyHint.style.display = "";
    } else {
      keyHint.style.display = "none";
    }
    baseRow.row.style.display = svc.requiresBaseUrl ? "" : "none";
    if (svc.models) {
      modelSelect.textContent = "";
      for (const m of svc.models) {
        const opt = document.createElement("option");
        opt.value = m.id;
        opt.textContent = m.label;
        modelSelect.append(opt);
      }
      modelSelect.style.display = "";
      modelInput.style.display = "none";
      detectBtn.style.display = svc.detectable ? "" : "none";
    } else {
      modelSelect.style.display = "none";
      modelInput.style.display = "";
      modelInput.placeholder = svc.modelPlaceholder || "model name";
      detectBtn.style.display = svc.detectable ? "" : "none";
    }
    hintEl.textContent = svc.hint || "";
    hintEl.style.display = svc.hint ? "" : "none";
    detectStatus.textContent = "";
  }

  async function detectModels() {
    const svc = serviceById(svcSelect.value);
    detectStatus.textContent = "detecting...";
    detectStatus.style.color = "#888";
    const baseUrl = svc.requiresBaseUrl
      ? baseInput.value.trim().replace(/\/+$/, "")
      : null; // server looks up the registry entry
    // For cloud services, backend uses the registry unless requiresBaseUrl
    // is set. Passing base_url only for "other" keeps the detect endpoint
    // simple.
    const body = {
      base_url: baseUrl || _registryBaseUrlForClient(svc.id),
      api_key: keyInput.value.trim() || undefined,
    };
    try {
      const r = await fetch("/promptchain/ai/detect-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      const models = data.models || [];
      if (models.length === 0) {
        detectStatus.textContent = data.error ? "✗ " + data.error : "no models found";
        detectStatus.style.color = "#e55";
        return;
      }
      modelsDatalist.textContent = "";
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        modelsDatalist.append(opt);
      }
      if (!modelInput.value && models[0]) modelInput.value = models[0].name;
      detectStatus.textContent = `✓ ${models.length} model${models.length === 1 ? "" : "s"}`;
      detectStatus.style.color = "#4ec96b";
    } catch (e) {
      detectStatus.textContent = "✗ " + (e.message || "failed");
      detectStatus.style.color = "#e55";
    }
  }

  svcSelect.addEventListener("change", () => applyService(svcSelect.value));
  detectBtn.addEventListener("click", detectModels);

  applyService("claude");

  return {
    el,
    applyConfig({ service, model, baseUrl, hasKey }) {
      svcSelect.value = service;
      applyService(service);
      baseInput.value = baseUrl || "";
      keyInput.value = "";
      keyInput.placeholder = hasKey
        ? "•••••••• (key saved — blank to keep)"
        : (serviceById(service).keyPlaceholder || "");
      const svc = serviceById(service);
      if (svc.models) modelSelect.value = model || svc.models[0].id;
      else modelInput.value = model || "";
    },
    clearKey(hasKey) {
      keyInput.value = "";
      const svc = serviceById(svcSelect.value);
      keyInput.placeholder = hasKey
        ? "•••••••• (key saved — blank to keep)"
        : (svc.keyPlaceholder || "");
    },
    readState() {
      const svc = serviceById(svcSelect.value);
      const model = svc.models ? modelSelect.value : modelInput.value.trim();
      return {
        service: svcSelect.value,
        model,
        key: keyInput.value.trim(),
        baseUrl: svc.requiresBaseUrl ? baseInput.value.trim().replace(/\/+$/, "") : "",
      };
    },
  };
}

// Mirror of the server's registry, used only so the Detect button in
// the UI can POST a concrete base_url without a round trip. Kept short;
// adding a new service means editing both places (one-time maintenance).
function _registryBaseUrlForClient(serviceId) {
  return {
    openai:     "https://api.openai.com/v1",
    grok:       "https://api.x.ai/v1",
    gemini:     "https://generativelanguage.googleapis.com/v1beta/openai",
    openrouter: "https://openrouter.ai/api/v1",
    deepseek:   "https://api.deepseek.com/v1",
    groq:       "https://api.groq.com/openai/v1",
    mistral:    "https://api.mistral.ai/v1",
  }[serviceId] || "";
}

function rowWithLabel(labelText) {
  const row = document.createElement("div");
  row.style.cssText = "display:flex;align-items:center;gap:8px;";
  const lbl = document.createElement("span");
  lbl.textContent = labelText;
  lbl.style.cssText = "font-size:12px;color:#888;min-width:70px;";
  row.append(lbl);
  return { row, label: lbl };
}

// ── local block ─────────────────────────────────────────────────────

// Reads an SSE POST stream, invoking onEvent for each parsed `data:` payload.
// Endpoints that fail before streaming return a JSON {error} with a non-2xx
// status instead — surfaced as a thrown Error.
async function streamSSE(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const data = frame.split("\n").find(l => l.startsWith("data:"));
      if (!data) continue;
      try { onEvent(JSON.parse(data.slice(5).trim())); } catch {}
    }
  }
}

function buildLocalBlock(onModelReady) {
  const el = document.createElement("div");
  el.style.cssText = "display:flex;flex-direction:column;gap:8px;padding:10px 12px;background:rgba(255,255,255,0.03);border-radius:4px;";

  // ── guided setup: detect Ollama, offer install + recommended-model pull ──
  const setup = document.createElement("div");
  setup.style.cssText = "display:flex;flex-direction:column;gap:8px;padding:10px;background:rgba(91,155,213,0.08);border:1px solid rgba(91,155,213,0.25);border-radius:4px;";

  const setupStatus = document.createElement("div");
  setupStatus.style.cssText = "font-size:12px;color:var(--input-text, #ddd);display:flex;flex-direction:column;gap:3px;";

  const setupActions = document.createElement("div");
  setupActions.style.cssText = "display:flex;align-items:center;gap:8px;flex-wrap:wrap;";

  const progressWrap = document.createElement("div");
  progressWrap.style.cssText = "display:none;flex-direction:column;gap:4px;";
  const progressTrack = document.createElement("div");
  progressTrack.style.cssText = "height:6px;border-radius:3px;background:rgba(255,255,255,0.12);overflow:hidden;";
  const progressBar = document.createElement("div");
  progressBar.style.cssText = "height:100%;width:0%;background:#5b9bd5;transition:width 0.2s;";
  progressTrack.append(progressBar);
  const progressText = document.createElement("div");
  progressText.style.cssText = "font-size:11px;color:#aaa;";
  progressWrap.append(progressTrack, progressText);

  setup.append(setupStatus, setupActions, progressWrap);

  // One status line: a fixed dot beside text that wraps on its own. Kept as a
  // single flex row so the dot never detaches from its label.
  function statusRow(ok, html) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:flex-start;gap:7px;line-height:1.45;";
    const d = document.createElement("span");
    d.style.cssText = `color:${ok ? "#4ec96b" : "#888"};flex:none;`;
    d.textContent = ok ? "●" : "○";
    const t = document.createElement("span");
    t.innerHTML = html;
    row.append(d, t);
    return row;
  }

  function showProgress(pct, text) {
    progressWrap.style.display = "flex";
    if (pct != null) progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    progressText.textContent = text || "";
  }
  function hideProgress() { progressWrap.style.display = "none"; }

  async function refreshSetup() {
    setupStatus.innerHTML = "<span style='color:#888;'>Checking local setup…</span>";
    setupActions.replaceChildren();
    let s;
    try {
      s = await (await fetch("/promptchain/ai/setup-status")).json();
    } catch {
      setupStatus.innerHTML = "<span style='color:#e55;'>Couldn't check setup status.</span>";
      manual.style.display = "flex";  // reveal so manual config is still possible
      return;
    }
    renderSetup(s);
    manual.style.display = "flex";
  }

  function renderSetup(s) {
    setupStatus.replaceChildren(
      statusRow(s.ollama_installed,
        `Ollama ${s.ollama_installed ? (s.ollama_running ? "running" : "installed") : "not installed"}`),
      statusRow(s.model_present,
        `Recommended model <code>${s.recommended_model}</code> — ${s.model_present ? "ready" : "not downloaded"}`),
    );
    setupActions.replaceChildren();

    if (s.model_present && s.ollama_running) {
      const ready = document.createElement("span");
      ready.style.cssText = "font-size:12px;color:#4ec96b;";
      ready.textContent = "✓ Ready to use the AI Assistant locally.";
      setupActions.append(ready);
      return;
    }

    if (!s.ollama_installed) {
      if (s.winget_available) {
        const installBtn = document.createElement("button");
        installBtn.textContent = "Install Ollama";
        installBtn.style.cssText = BTN_STYLE + "background:#0e639c;color:#fff;";
        installBtn.addEventListener("click", () => installOllama(installBtn));
        setupActions.append(installBtn);
        const note = document.createElement("span");
        note.style.cssText = "font-size:11px;color:#888;";
        note.textContent = "via winget — may ask for permission.";
        setupActions.append(note);
      } else {
        const link = document.createElement("a");
        link.href = "https://ollama.com/download";
        link.target = "_blank";
        link.textContent = "Download Ollama →";
        link.style.cssText = "font-size:12px;color:#5b9bd5;";
        setupActions.append(link);
        const note = document.createElement("span");
        note.style.cssText = "font-size:11px;color:#888;";
        note.textContent = "then click Re-check.";
        setupActions.append(note);
      }
    } else if (!s.model_present) {
      const pullBtn = document.createElement("button");
      pullBtn.textContent = `Download ${s.recommended_model}`;
      pullBtn.style.cssText = BTN_STYLE + "background:#0e639c;color:#fff;";
      pullBtn.addEventListener("click", () => pullModel(pullBtn, s.recommended_model));
      setupActions.append(pullBtn);
      const note = document.createElement("span");
      note.style.cssText = "font-size:11px;color:#888;";
      note.textContent = "~6 GB download.";
      setupActions.append(note);
    }

    const recheck = document.createElement("button");
    recheck.textContent = "Re-check";
    recheck.style.cssText = BTN_STYLE;
    recheck.addEventListener("click", refreshSetup);
    setupActions.append(recheck);

    // Already-running non-Ollama servers (LM Studio, llama.cpp, …). We can
    // use these over the OpenAI-compat path even though we can't manage them.
    for (const srv of (s.detected_servers || [])) {
      const altRow = document.createElement("div");
      altRow.style.cssText = "display:flex;align-items:center;gap:8px;font-size:12px;color:#aaa;margin-top:2px;";
      const text = document.createElement("span");
      text.innerHTML = `Found <strong>${srv.label}</strong> on <code>:${srv.port}</code>`;
      const useBtn = document.createElement("button");
      useBtn.textContent = "Use it";
      useBtn.style.cssText = BTN_STYLE;
      useBtn.addEventListener("click", () => useDetectedServer(useBtn, srv));
      altRow.append(text, useBtn);
      setupActions.append(altRow);
    }
  }

  async function useDetectedServer(btn, srv) {
    btn.disabled = true;
    btn.textContent = "Connecting…";
    urlInput.value = srv.base_url;
    await detectLocal();
    const model = modelInput.value;
    if (model) {
      onModelReady?.(model);
      btn.textContent = "✓ Connected";
    } else {
      btn.disabled = false;
      btn.textContent = "Use it";
    }
  }

  async function installOllama(btn) {
    btn.disabled = true;
    btn.textContent = "Installing…";
    showProgress(null, "Installing Ollama via winget…");
    try {
      await streamSSE("/promptchain/ai/install-ollama", {}, (evt) => {
        if (evt.line) progressText.textContent = evt.line;
        if (evt.error) progressText.textContent = "✗ " + evt.error;
        if (evt.done) progressText.textContent = evt.ok ? "✓ Installed." : "✗ Install failed.";
      });
    } catch (e) {
      progressText.textContent = "✗ " + (e.message || "install failed");
    } finally {
      btn.disabled = false;
      btn.textContent = "Install Ollama";
      setTimeout(hideProgress, 1500);
      refreshSetup();
    }
  }

  async function pullModel(btn, model) {
    btn.disabled = true;
    btn.textContent = "Downloading…";
    showProgress(0, "Starting download…");
    let ok = false;
    try {
      await streamSSE("/promptchain/ai/pull-model", { model }, (evt) => {
        if (evt.error) { progressText.textContent = "✗ " + evt.error; return; }
        if (evt.total && evt.completed != null) {
          showProgress((evt.completed / evt.total) * 100,
            `${evt.status || "downloading"} — ${fmtBytes(evt.completed)} / ${fmtBytes(evt.total)}`);
        } else if (evt.status) {
          progressText.textContent = evt.status;
        }
        if (evt.done) ok = true;
      });
    } catch (e) {
      progressText.textContent = "✗ " + (e.message || "download failed");
    } finally {
      btn.disabled = false;
      btn.textContent = `Download ${model}`;
      if (ok) {
        showProgress(100, "✓ Downloaded.");
        urlInput.value = urlInput.value.trim() || "http://localhost:11434/v1";
        setModel(model);
        onModelReady?.(model);
      }
      setTimeout(hideProgress, 1500);
      refreshSetup();
    }
  }

  el.append(setup);

  const urlRow = rowWithLabel("Base URL");
  const urlInput = document.createElement("input");
  urlInput.type = "text";
  urlInput.spellcheck = false;
  urlInput.autocomplete = "off";
  urlInput.style.cssText = INPUT_STYLE + "flex:1;font-family:monospace;";
  urlInput.placeholder = "http://localhost:11434/v1";
  urlRow.row.append(urlInput);

  const urlHint = document.createElement("div");
  urlHint.style.cssText = "font-size:11px;color:#888;padding-left:78px;";
  urlHint.innerHTML = "Ollama: <code>http://localhost:11434/v1</code> · llama.cpp: <code>:8080/v1</code> · LM Studio: <code>:1234/v1</code>";

  const modelRow = rowWithLabel("Model");
  const modelSelect = document.createElement("select");
  modelSelect.style.cssText = INPUT_STYLE + "flex:1;font-family:monospace;";
  const detectBtn = document.createElement("button");
  detectBtn.textContent = "Detect";
  detectBtn.style.cssText = BTN_STYLE;
  modelRow.row.append(modelSelect, detectBtn);

  const detectStatus = document.createElement("div");
  detectStatus.style.cssText = "font-size:11px;color:#888;padding-left:78px;";

  // Auto-start toggle — when Ollama isn't reachable on panel open, the
  // probe spawns `ollama serve` once instead of just showing an error.
  // Off for users who manage Ollama externally (docker, systemd, etc.).
  const autoStartRow = document.createElement("label");
  autoStartRow.style.cssText = "display:flex;align-items:center;gap:8px;padding-left:78px;cursor:pointer;";
  const autoStartInput = document.createElement("input");
  autoStartInput.type = "checkbox";
  autoStartInput.checked = true;
  const autoStartLabel = document.createElement("span");
  autoStartLabel.textContent = "Auto-start Ollama when offline";
  autoStartLabel.style.cssText = "font-size:12px;color:var(--input-text, #ddd);";
  autoStartRow.append(autoStartInput, autoStartLabel);
  const autoStartHint = document.createElement("div");
  autoStartHint.style.cssText = "font-size:11px;color:#888;padding-left:78px;";
  autoStartHint.textContent = "If PromptChain can't reach Ollama, it'll try `ollama serve` once before erroring.";

  // Manual config (URL / model / auto-start) stays hidden until detection
  // resolves, so the user never edits these before their state is known.
  const manual = document.createElement("div");
  manual.style.cssText = "display:none;flex-direction:column;gap:8px;";
  manual.append(urlRow.row, urlHint, modelRow.row, detectStatus, autoStartRow, autoStartHint);
  el.append(manual);

  // The select reflects what the server returned from /api/tags. Before
  // Detect runs we only have the saved model name from config; render it
  // as a single option so the control doesn't look empty on first paint.
  function setModel(name) {
    modelSelect.textContent = "";
    if (!name) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "click Detect to list installed models";
      placeholder.disabled = true;
      placeholder.selected = true;
      modelSelect.append(placeholder);
      return;
    }
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    modelSelect.append(opt);
    modelSelect.value = name;
  }
  setModel("");

  async function detectLocal() {
    detectStatus.textContent = "detecting...";
    detectStatus.style.color = "#888";
    try {
      const r = await fetch("/promptchain/ai/detect-models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: urlInput.value.trim() }),
      });
      const data = await r.json();
      const models = data.models || [];
      if (models.length === 0) {
        detectStatus.textContent = data.error ? "✗ " + data.error : "no models found";
        detectStatus.style.color = "#e55";
        return;
      }
      const previous = modelSelect.value;
      modelSelect.textContent = "";
      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        opt.textContent = m.vision ? `${m.name} (vision)` : m.name;
        modelSelect.append(opt);
      }
      // Saved model no longer installed — keep it visible so the user
      // can tell what happened, but flag it.
      if (previous && !models.some(m => m.name === previous)) {
        const opt = document.createElement("option");
        opt.value = previous;
        opt.textContent = `${previous} (not installed)`;
        modelSelect.insertBefore(opt, modelSelect.firstChild);
      }
      modelSelect.value = previous && [...modelSelect.options].some(o => o.value === previous)
        ? previous
        : models[0].name;
      detectStatus.textContent = `✓ ${models.length} model${models.length === 1 ? "" : "s"} installed`;
      detectStatus.style.color = "#4ec96b";
    } catch (e) {
      detectStatus.textContent = "✗ " + (e.message || "failed");
      detectStatus.style.color = "#e55";
    }
  }
  detectBtn.addEventListener("click", detectLocal);

  // Keep the external .value get/set shape the outer renderAiSetting uses.
  const modelInput = {
    get value() { return modelSelect.value || ""; },
    set value(v) { setModel(v); },
  };

  // Detection is scoped to this block (its status area shows its own
  // "checking" state) — it never blocks the None/Cloud choices above.
  refreshSetup();
  return { el, urlInput, modelInput, autoStartInput, refreshSetup };
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}
