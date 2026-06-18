// Tag source dropdown — footer element for selecting which tag DBs to use for autocomplete.
// Sources are ordered by priority (drag to reorder). Auto-applies from model config
// on first connect; manual changes override auto-apply for the session.
// Config persists to node.properties and syncs across all PromptChain nodes in the workflow.

import { app } from "../../../scripts/app.js";
import { NODE_TYPE } from "./config.js";

let _config = {
  sources: [],            // enabled source names in priority order
  format: "spaces",       // "spaces" or "underscores" for tag insertion
  prompt_style: "tags",   // "tags" or "natural" — drives TagBuilder default mode
};

let _availableSources = [];
let _sourcesLoaded = false;
let _manuallySet = false;    // true once user explicitly changes sources this session
let _lastAutoHash = null;    // last model hash we auto-applied from
let _restoredFromWorkflow = false;  // true if sources were restored from node.properties

// ── config API ─────────────────────────────────────────────────────

export function getTagSourceConfig() {
  return { ..._config };
}

export function setTagSourceConfig(partial, { auto = false } = {}) {
  if (partial.sources !== undefined) _config.sources = [...partial.sources];
  if (partial.format !== undefined) _config.format = partial.format;
  if (partial.prompt_style !== undefined) _config.prompt_style = partial.prompt_style;
  if (!auto) _manuallySet = true;
  _syncToAllNodes();
  window.dispatchEvent(new CustomEvent("pcr-tag-config-changed", { detail: _config }));
}

function _syncToAllNodes() {
  for (const n of app.graph?._nodes || []) {
    if ((n.comfyClass || n.type) !== NODE_TYPE) continue;
    if (!n.properties) n.properties = {};
    n.properties.pcrTagSources = [..._config.sources];
    n.properties.pcrTagFormat = _config.format;
    n.properties.pcrPromptStyle = _config.prompt_style;
  }
}

function _restoreFromNode(node) {
  const sources = node.properties?.pcrTagSources;
  const format = node.properties?.pcrTagFormat;
  const promptStyle = node.properties?.pcrPromptStyle;
  let restored = false;
  if (sources?.length) {
    _config.sources = [...sources];
    _config.format = format || "spaces";
    restored = true;
  }
  if (promptStyle) {
    _config.prompt_style = promptStyle;
    restored = true;
  }
  if (restored) _restoredFromWorkflow = true;
  return restored;
}

async function loadAvailableSources() {
  if (_sourcesLoaded) return _availableSources;
  try {
    const res = await fetch("/promptchain/tags/sources");
    const data = await res.json();
    _availableSources = data.sources || [];
    _sourcesLoaded = true;
  } catch (e) {
    console.error("[PromptChain] Failed to load tag sources:", e);
  }
  return _availableSources;
}

// ── auto-apply from model config ──────────────────────────────────

export async function autoApplyTagSources(hash) {
  if (!hash) return;
  // Same model — nothing to do
  if (hash === _lastAutoHash) return;
  _lastAutoHash = hash;
  // User explicitly chose sources this session — don't override
  if (_manuallySet) return;
  // Workflow had saved tag sources — respect on initial load, then clear so
  // switching to a different model can auto-apply its own defaults
  if (_restoredFromWorkflow) {
    _restoredFromWorkflow = false;
    return;
  }
  try {
    const res = await fetch(`/promptchain/models/settings/${hash}`);
    if (!res.ok) return;
    const settings = await res.json();
    const partial = {};
    if (settings.tag_sources?.length) partial.sources = settings.tag_sources;
    if (settings.tag_format) partial.format = settings.tag_format;
    if (settings.prompt_style) partial.prompt_style = settings.prompt_style;
    if (Object.keys(partial).length) {
      setTagSourceConfig(partial, { auto: true });
    }
  } catch {}
}

// Reset manual override flag when model changes — allows auto-apply for the new model
export function resetTagAutoApply() {
  _manuallySet = false;
  _lastAutoHash = null;
}

// ── dropdown UI ────────────────────────────────────────────────────

export function createTagsDropdown({ node, getModelInfo } = {}) {
  const el = document.createElement("div");
  el.className = "pcr-tags-dropdown";

  const label = document.createElement("span");
  label.className = "pcr-tags-dropdown-label";
  label.textContent = "No Tags";
  label.title = "Configure tag autocomplete sources";
  el.appendChild(label);

  const menu = document.createElement("div");
  menu.className = "pcr-tags-menu";
  menu.style.display = "none";
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(menu);

  let isOpen = false;

  function close() {
    if (!isOpen) return;
    isOpen = false;
    menu.style.display = "none";
    label.classList.remove("pcr-tags-open");
  }

  async function open() {
    await loadAvailableSources();
    populateMenu(menu, updateLabel, getModelInfo);
    isOpen = true;
    menu.style.display = "block";
    label.classList.add("pcr-tags-open");
    // position above the label
    const rect = label.getBoundingClientRect();
    menu.style.left = `${rect.left}px`;
    menu.style.bottom = `${window.innerHeight - rect.top + 4}px`;
  }

  label.addEventListener("click", (e) => {
    e.stopPropagation();
    if (isOpen) close(); else open();
  });

  // close on outside click — capture phase so isolation/stopPropagation can't block it
  const ac = new AbortController();
  document.addEventListener("pointerdown", (e) => {
    if (isOpen && !el.contains(e.target) && !menu.contains(e.target)) close();
  }, { capture: true, signal: ac.signal });

  function updateLabel() {
    const s = _config.sources;
    if (s.length === 0) {
      label.textContent = "No Tags";
      label.className = "pcr-tags-dropdown-label";
    } else if (s.length === 1) {
      label.textContent = `Tags: ${s[0]}`;
      label.className = "pcr-tags-dropdown-label pcr-tags-active";
    } else {
      label.textContent = `Tags: ${s.length} sources`;
      label.className = "pcr-tags-dropdown-label pcr-tags-active";
    }
  }

  function restore() {
    if (node && _restoreFromNode(node)) {
      updateLabel();
    }
  }

  loadAvailableSources().then(updateLabel);
  window.addEventListener("pcr-tag-config-changed", updateLabel, { signal: ac.signal });

  return { element: el, updateLabel, restore, cleanup: () => { ac.abort(); menu.remove(); } };
}

// ── menu population ────────────────────────────────────────────────

function populateMenu(menu, updateLabel, getModelInfo) {
  menu.innerHTML = "";
  const active = new Set(_config.sources);

  // header
  const header = document.createElement("div");
  header.className = "pcr-tags-menu-header";

  const title = document.createElement("span");
  title.textContent = "Tag Sources";
  header.appendChild(title);

  const fmtLabel = document.createElement("label");
  fmtLabel.className = "pcr-tags-fmt-toggle";
  const fmtCb = document.createElement("input");
  fmtCb.type = "checkbox";
  fmtCb.checked = _config.format === "underscores";
  fmtCb.addEventListener("change", (e) => {
    e.stopPropagation();
    setTagSourceConfig({ format: fmtCb.checked ? "underscores" : "spaces" });
  });
  fmtLabel.appendChild(fmtCb);
  fmtLabel.appendChild(document.createTextNode(" Underscores"));
  header.appendChild(fmtLabel);
  menu.appendChild(header);

  // prompt-mode toggle — picks how the AI Assistant + tag-builder shape
  // their output (tag-form `1girl, blue eyes` vs prose `a girl with blue
  // eyes`). Auto-applied from the active model's config when the user
  // hasn't overridden; explicit pick here sticks for the session.
  // The sliding-indicator pattern keeps both options visible and animates
  // the active highlight between them — preserves spatial continuity when
  // flipping mode, vs a binary checkbox or radios.
  const modeRow = document.createElement("div");
  modeRow.className = "pcr-tags-mode-toggle";
  if (_config.prompt_style === "natural") modeRow.classList.add("is-natural");

  const slider = document.createElement("div");
  slider.className = "pcr-tags-mode-slider";
  modeRow.appendChild(slider);

  const ICONS = {
    tags: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',
    natural: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="15" y2="12"/><line x1="3" y1="18" x2="18" y2="18"/></svg>',
  };
  const SAMPLES = { tags: "tag1, tag2", natural: "a sentence" };

  for (const opt of [
    { id: "tags", label: "Tags" },
    { id: "natural", label: "Natural" },
  ]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pcr-tags-mode-btn";
    btn.dataset.mode = opt.id;
    if (_config.prompt_style === opt.id) btn.classList.add("active");
    btn.innerHTML = `
      <span class="pcr-tags-mode-btn-row">
        <span class="pcr-tags-mode-icon">${ICONS[opt.id]}</span>
        <span class="pcr-tags-mode-label">${opt.label}</span>
      </span>
      <span class="pcr-tags-mode-sample">${SAMPLES[opt.id]}</span>
    `;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (_config.prompt_style === opt.id) return;
      setTagSourceConfig({ prompt_style: opt.id });
      // Toggle the slider class without re-rendering the menu so the
      // CSS transition runs. Also update active class on each button.
      modeRow.classList.toggle("is-natural", opt.id === "natural");
      modeRow.querySelectorAll(".pcr-tags-mode-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.mode === opt.id);
      });
    });
    modeRow.appendChild(btn);
  }
  menu.appendChild(modeRow);

  // source list
  const list = document.createElement("div");
  list.className = "pcr-tags-source-list";

  function addRow(name, enabled, idx) {
    const info = _availableSources.find(s => s.name === name);
    const count = info?.count || 0;

    const row = document.createElement("div");
    row.className = `pcr-tags-source-row ${enabled ? "enabled" : ""}`;
    row.draggable = enabled;
    row.dataset.source = name;

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = enabled;
    cb.addEventListener("click", (e) => e.stopPropagation());
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      let next = [..._config.sources];
      if (cb.checked) { if (!next.includes(name)) next.push(name); }
      else { next = next.filter(s => s !== name); }
      setTagSourceConfig({ sources: next });
      populateMenu(menu, updateLabel, getModelInfo);
      updateLabel();
    });
    row.appendChild(cb);

    if (enabled) {
      const handle = document.createElement("span");
      handle.className = "pcr-tags-drag-handle";
      handle.textContent = "≡";
      row.appendChild(handle);
    }

    const nameEl = document.createElement("span");
    nameEl.className = "pcr-tags-source-name";
    nameEl.textContent = name;
    row.appendChild(nameEl);

    if (count > 0) {
      const countEl = document.createElement("span");
      countEl.className = "pcr-tags-source-count";
      countEl.textContent = count.toLocaleString();
      row.appendChild(countEl);
    }

    if (enabled) {
      const badge = document.createElement("span");
      badge.className = "pcr-tags-priority-badge";
      badge.textContent = `#${idx + 1}`;
      row.appendChild(badge);

      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", name);
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => row.classList.remove("dragging"));
      row.addEventListener("dragover", (e) => {
        e.preventDefault();
        const rect = row.getBoundingClientRect();
        const inTopHalf = e.clientY < rect.top + rect.height / 2;
        row.classList.remove("drag-over-top", "drag-over-bottom");
        row.classList.add(inTopHalf ? "drag-over-top" : "drag-over-bottom");
      });
      row.addEventListener("dragleave", () => row.classList.remove("drag-over-top", "drag-over-bottom"));
      row.addEventListener("drop", (e) => {
        e.preventDefault();
        const dropAbove = row.classList.contains("drag-over-top");
        row.classList.remove("drag-over-top", "drag-over-bottom");
        const dragged = e.dataTransfer.getData("text/plain");
        if (dragged === name) return;
        const next = [..._config.sources];
        const di = next.indexOf(dragged);
        const ti = next.indexOf(name);
        if (di === -1 || ti === -1) return;
        next.splice(di, 1);
        let insertAt = dropAbove ? ti : ti + 1;
        if (di < ti) insertAt--;
        if (insertAt === di) return;
        next.splice(insertAt, 0, dragged);
        setTagSourceConfig({ sources: next });
        populateMenu(menu, updateLabel, getModelInfo);
        updateLabel();
      });
    }

    list.appendChild(row);
  }

  // enabled first, then disabled
  _config.sources.forEach((s, i) => addRow(s, true, i));
  _availableSources.filter(s => !active.has(s.name)).forEach(s => addRow(s.name, false, -1));
  menu.appendChild(list);

  const info = document.createElement("div");
  info.className = "pcr-tags-menu-info";
  info.textContent = "Drag to reorder priority";
  menu.appendChild(info);

  // Save/Restore as model default
  const modelInfo = getModelInfo?.();
  if (modelInfo?.hash) {
    const btnRow = document.createElement("div");
    btnRow.className = "pcr-tags-btn-row";

    const restoreBtn = document.createElement("button");
    restoreBtn.className = "pcr-tags-btn pcr-tags-btn-restore";
    restoreBtn.textContent = "Restore Defaults";
    restoreBtn.disabled = true;

    const saveBtn = document.createElement("button");
    saveBtn.className = "pcr-tags-btn pcr-tags-btn-save";
    saveBtn.textContent = "Save as Default";
    saveBtn.disabled = true;

    // Fetch saved tag sources for this model
    fetch(`/promptchain/models/settings/${modelInfo.hash}`)
      .then(r => r.ok ? r.json() : null)
      .then(saved => {
        const savedSources = saved?.tag_sources;
        if (!savedSources) {
          // No saved tags yet — enable save
          saveBtn.disabled = false;
          return;
        }

        // Check if current differs from saved
        const currentStr = JSON.stringify(_config.sources);
        const savedStr = JSON.stringify(savedSources);
        const deviated = currentStr !== savedStr;
        saveBtn.disabled = !deviated;
        restoreBtn.disabled = !deviated;
      })
      .catch(() => {});

    saveBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving...";
      try {
        // Merge into existing config
        const res = await fetch(`/promptchain/models/settings/${modelInfo.hash}`);
        const existing = res.ok ? await res.json() : { display_name: modelInfo.filename, architecture: modelInfo.architecture };
        existing.tag_sources = [..._config.sources];
        await fetch(`/promptchain/models/settings/${modelInfo.hash}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(existing),
        });
        saveBtn.textContent = "Saved";
        restoreBtn.disabled = true;
        setTimeout(() => { saveBtn.textContent = "Save as Default"; }, 1000);
      } catch {
        saveBtn.textContent = "Error";
        saveBtn.disabled = false;
      }
    });

    restoreBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const res = await fetch(`/promptchain/models/settings/${modelInfo.hash}`);
        if (!res.ok) return;
        const saved = await res.json();
        if (saved.tag_sources) {
          setTagSourceConfig({ sources: saved.tag_sources });
          populateMenu(menu, updateLabel, getModelInfo);
          updateLabel();
        }
      } catch {}
    });

    btnRow.appendChild(restoreBtn);
    btnRow.appendChild(saveBtn);
    menu.appendChild(btnRow);
  }
}
