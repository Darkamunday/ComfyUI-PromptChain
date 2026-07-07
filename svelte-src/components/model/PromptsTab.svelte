<script>
  // PromptsTab — prompt snippets by scope, add/edit/delete, category dropdowns.

  import "./model-panel-shared.css";

  let {
    modelInfo = {},
    savedConfig = null,
    getEditor,
    onClose,
  } = $props();

  const arch = savedConfig?.architecture || modelInfo.architecture || "";
  const family = savedConfig?.family || "";
  const modelName = savedConfig?.model_name || modelInfo.filename || "";
  const version = savedConfig?.version || "";

  let prompts = $state([]);
  let loading = $state(true);
  let editMode = $state(false);

  // Add form
  let newName = $state("");
  let newCategory = $state("");
  let newSubcategory = $state("");
  let newText = $state("");
  let newScope = $state("global");
  let newSlot = $state("");

  // Dropdown state
  let openDropdown = $state(null);

  function loadPrompts() {
    loading = true;
    const params = new URLSearchParams();
    if (arch) params.set("arch", arch);
    if (family) params.set("family", family);
    if (modelName) params.set("name", modelName);
    if (modelInfo.hash) params.set("hash", modelInfo.hash);

    fetch(`/promptchain/prompts/list?${params}`)
      .then(r => r.json())
      .then(({ prompts: p }) => { prompts = p || []; loading = false; })
      .catch(() => { prompts = []; loading = false; });
  }

  loadPrompts();

  let grouped = $derived.by(() => {
    const groups = new Map();
    for (const p of prompts) {
      const category = (p.category || "General").trim();
      const subcategory = (p.subcategory || "").trim();
      if (!groups.has(category)) {
        groups.set(category, {
          category,
          count: 0,
          hasSubcategories: false,
          subgroups: new Map(),
        });
      }
      const group = groups.get(category);
      const subKey = subcategory || "General";
      if (subcategory) group.hasSubcategories = true;
      if (!group.subgroups.has(subKey)) group.subgroups.set(subKey, []);
      group.subgroups.get(subKey).push(p);
      group.count += 1;
    }
    return [...groups.values()].map(group => ({
      ...group,
      subgroups: [...group.subgroups.entries()].map(([subcategory, items]) => ({ subcategory, items })),
    }));
  });

  // Mirrors buildPromptInsertion in js/lib/autocomplete.js (@prompt source):
  // EMPTY editor gets the full template ("// Your Tags" scaffold included,
  // caret on the {cursor} slot); an editor with content already HAS the
  // user's tags, so everything up to and including {cursor} is dropped and
  // only the style + negative sections are inserted.
  function insertPromptText(text) {
    const editor = getEditor();
    if (!editor) return;
    const { from, to } = editor.state.selection.main;
    const docText = editor.state.doc.toString();
    const cursorIdx = text.indexOf("{cursor}");

    let insertText, anchor;
    if (!docText.trim()) {
      insertText = text.replace("{cursor}", "");
      anchor = cursorIdx >= 0 ? from + cursorIdx : from + insertText.length;
    } else {
      const after = cursorIdx >= 0 ? text.slice(cursorIdx + "{cursor}".length) : text;
      const trimmedAfter = after.replace(/^\s+/, "");
      // Caret lands at the end of the positive section (just before the
      // Negative Prompt block), or end of insertion without one.
      const negMatch = trimmedAfter.match(/\n+Negative Prompt:/i);
      let cursorWithinAfter;
      if (negMatch) {
        let idx = negMatch.index;
        while (idx > 0 && /\s/.test(trimmedAfter[idx - 1])) idx--;
        cursorWithinAfter = idx;
      } else {
        cursorWithinAfter = trimmedAfter.length;
      }
      const before = docText.slice(0, from);
      const trailNewlines = (before.match(/\n*$/) || [""])[0].length;
      const separator = before.trim() ? "\n".repeat(Math.max(0, 2 - trailNewlines)) : "";
      insertText = separator + trimmedAfter;
      anchor = from + separator.length + cursorWithinAfter;
    }

    editor.dispatch({
      changes: { from, to, insert: insertText },
      selection: { anchor },
    });
    editor.focus();
    onClose();
  }

  function handleDelete(p) {
    fetch(`/promptchain/prompts/${p.id}`, { method: "DELETE" })
      .then(() => loadPrompts());
  }

  async function handleAdd() {
    if (!newName.trim() || !newText.trim()) return;

    const scope = { type: newScope };
    if (newScope === "architecture") scope.architecture = arch;
    if (newScope === "family") { scope.architecture = arch; scope.family = family; }
    if (newScope === "model") scope.model_name = modelName;
    if (newScope === "version") scope.model_hash = modelInfo.hash;

    await fetch("/promptchain/prompts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newName.trim(),
        text: newText.trim(),
        scope,
        category: newCategory.trim() || undefined,
        subcategory: newSubcategory.trim() || undefined,
        slot: newSlot || undefined,
      }),
    });
    newName = "";
    newText = "";
    newCategory = "";
    newSubcategory = "";
    loadPrompts();
  }

  // Float the menu above panel overflow and ancestor CSS transforms. The model
  // panel sits inside ComfyUI's LiteGraph canvas; ancestor transforms make
  // position:fixed resolve against the wrong containing block, so we promote
  // the menu to the browser top-layer via the Popover API. Side and shift are
  // recomputed on every update to flip when there isn't room.
  function anchorToButton(menu, onClose) {
    const btn = menu.previousElementSibling;
    if (!btn) return;
    menu.showPopover?.();
    const gap = 4;
    const update = () => {
      const rect = btn.getBoundingClientRect();
      const menuH = menu.offsetHeight;
      const menuW = menu.offsetWidth;
      const spaceBelow = window.innerHeight - rect.bottom;
      const openUp = spaceBelow < menuH + gap && rect.top > spaceBelow;

      if (openUp) {
        menu.style.top = "auto";
        menu.style.bottom = window.innerHeight - rect.top + gap + "px";
      } else {
        menu.style.bottom = "auto";
        menu.style.top = rect.bottom + gap + "px";
      }

      const maxLeft = window.innerWidth - menuW - 4;
      menu.style.left = Math.max(4, Math.min(rect.left, maxLeft)) + "px";
    };
    // Clicks on any dropdown trigger are handled by that button's own toggle,
    // so we let them through here — otherwise we'd close-then-reopen.
    const onDocClick = (e) => {
      if (menu.contains(e.target)) return;
      if (e.target.closest?.(".pcr-prompt-dropdown-btn")) return;
      onClose?.();
    };
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKey);
    return {
      destroy() {
        window.removeEventListener("scroll", update, true);
        window.removeEventListener("resize", update);
        document.removeEventListener("click", onDocClick);
        document.removeEventListener("keydown", onKey);
      },
    };
  }

  let scopeOptions = $derived.by(() => {
    const opts = [
      { value: "global", label: "Global" },
      { value: "architecture", label: `Arch (${arch || "any"})` },
    ];
    if (family) opts.push({ value: "family", label: `Family (${family})` });
    if (modelName) opts.push({ value: "model", label: `Model (${modelName})` });
    opts.push({ value: "version", label: version ? `Ver (${version})` : "This version" });
    return opts;
  });
</script>

<div class="pcr-model-panel-body">
  {#if loading}
    <div class="pcr-model-panel-empty">Loading...</div>
  {:else if !prompts.length && !editMode}
    <div class="pcr-model-panel-empty">No prompt presets yet</div>
  {:else}
    {#if prompts.length}
      <div class="pcr-model-panel-section-title">Prompt Templates</div>
    {/if}
    <div class="pcr-prompt-grid">
      {#each grouped as group}
        {#if group.count > 1}
          <!-- Dropdown group -->
          <div class="pcr-template-dropdown-container">
            <button class="pcr-prompt-btn pcr-prompt-dropdown-btn"
              class:pcr-open={openDropdown === group.category}
              onclick={(e) => {
                e.stopPropagation();
                openDropdown = openDropdown === group.category ? null : group.category;
              }}>
              {group.category} <span class="pcr-prompt-dropdown-arrow">▼</span>
            </button>
            {#if openDropdown === group.category}
              <div class="pcr-prompt-dropdown-menu" popover="manual"
                use:anchorToButton={() => { openDropdown = null; }}>
                {#each group.subgroups as subgroup}
                  {#if group.hasSubcategories}
                    <div class="pcr-prompt-subcategory-header">{subgroup.subcategory}</div>
                  {/if}
                  {#each subgroup.items as p}
                    <div class="pcr-prompt-dropdown-item"
                      title={p.text || ""}
                      style={editMode ? "display:flex;justify-content:space-between;align-items:center" : ""}
                      onclick={(e) => {
                        if (!editMode) {
                          e.stopPropagation();
                          openDropdown = null;
                          insertPromptText(p.text || "");
                        }
                      }}>
                      {#if editMode}
                        <span>{p.name}</span>
                        <span class="pcr-prompt-dropdown-item-del"
                          onclick={(e) => { e.stopPropagation(); handleDelete(p); }}>×</span>
                      {:else}
                        {p.name}
                      {/if}
                    </div>
                  {/each}
                {/each}
              </div>
            {/if}
          </div>
        {:else}
          <!-- Flat buttons -->
          {#each group.subgroups as subgroup}
            {#each subgroup.items as p}
              <button class="pcr-prompt-btn"
                title={p.text || ""}
                onclick={(e) => {
                  e.stopPropagation();
                  if (editMode) {
                    handleDelete(p);
                  } else {
                    insertPromptText(p.text || "");
                  }
                }}>
                {#if editMode}
                  {p.name} <span class="pcr-prompt-del-badge">×</span>
                {:else}
                  {p.name}
                {/if}
              </button>
            {/each}
          {/each}
        {/if}
      {/each}
    </div>
  {/if}

  {#if editMode}
    <div class="pcr-tpl-save-section">
      <div class="pcr-tpl-save-row">
        <input type="text" class="pcr-tpl-save-name" placeholder="Name..."
          bind:value={newName} />
        <input type="text" class="pcr-tpl-save-name" placeholder="Category..."
          style="max-width:120px" bind:value={newCategory} />
        <input type="text" class="pcr-tpl-save-name" placeholder="Subcategory..."
          style="max-width:120px" bind:value={newSubcategory} />
      </div>
      <div class="pcr-tpl-save-row">
        <select class="pcr-tpl-save-scope" bind:value={newScope}>
          {#each scopeOptions as opt}
            <option value={opt.value}>{opt.label}</option>
          {/each}
        </select>
        <select class="pcr-tpl-save-scope" bind:value={newSlot}>
          <option value="">Any slot</option>
          <option value="positive">Positive</option>
          <option value="negative">Negative</option>
        </select>
      </div>
      <textarea class="pcr-tpl-save-name" bind:value={newText}
        placeholder="Prompt text... {'{cursor}'} marks cursor position"
        style="min-height:48px;resize:vertical;font-family:monospace;"></textarea>
      <button class="pcr-model-panel-save" onclick={handleAdd}>Add Prompt</button>
    </div>
  {/if}

  <div style="border-top:1px solid #333;margin-top:8px;padding-top:8px;">
    <button class="pcr-model-panel-apply"
      onclick={(e) => { e.stopPropagation(); editMode = !editMode; openDropdown = null; loadPrompts(); }}>
      {editMode ? "Done" : "Edit Prompts"}
    </button>
  </div>
</div>

<style>
  .pcr-model-panel-section-title {
    font-size: 11px;
    font-weight: 600;
    color: #4f97cf;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  /* save form (shared with TemplatesTab) */
  .pcr-tpl-save-section {
    border-top: 1px solid #333;
    padding-top: 8px;
    margin-top: 4px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .pcr-tpl-save-row {
    display: flex;
    gap: 6px;
    min-width: 0;
  }
  .pcr-tpl-save-name {
    flex: 1;
    min-width: 0;
    font-size: 12px;
    padding: 4px 8px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid #444;
    border-radius: 4px;
    color: #ddd;
    outline: none;
  }
  .pcr-tpl-save-name:focus { border-color: #4fc3f7; }
  .pcr-tpl-save-name::placeholder { color: #666; }
  .pcr-tpl-save-scope {
    font-size: 11px;
    padding: 3px 6px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid #444;
    border-radius: 4px;
    color: #aaa;
    cursor: pointer;
    appearance: none;
    -webkit-appearance: none;
    min-width: 0;
  }
  .pcr-tpl-save-scope:focus { border-color: #4fc3f7; outline: none; }

  /* prompt button grid */
  .pcr-prompt-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 20px;
  }
  .pcr-prompt-btn {
    padding: 6px 10px;
    background: rgb(26 41 53 / 80%);
    border: 1px solid #6d8ebba6;
    border-radius: 4px;
    color: #ccc;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .pcr-prompt-btn:hover {
    background: rgba(79, 195, 247, 0.2);
    border-color: #4fc3f7;
    color: #4fc3f7;
  }
  .pcr-prompt-dropdown-btn {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .pcr-prompt-dropdown-arrow {
    font-size: 8px;
    opacity: 0.6;
    transition: transform 0.15s;
  }
  .pcr-prompt-dropdown-btn.pcr-open .pcr-prompt-dropdown-arrow {
    transform: rotate(180deg);
  }
  .pcr-prompt-dropdown-btn.pcr-open {
    background: rgba(79, 195, 247, 0.2);
    border-color: #4fc3f7;
    color: #4fc3f7;
  }
  .pcr-prompt-dropdown-menu {
    position: fixed;
    inset: auto;
    margin: 0;
    background: rgba(30, 30, 30, 0.98);
    border: 1px solid #555;
    border-radius: 6px;
    padding: 4px 0;
    max-height: 280px;
    overflow-y: auto;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
    color: inherit;
  }
  .pcr-prompt-dropdown-item {
    padding: 8px 12px;
    font-size: 12px;
    color: #bbb;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .pcr-prompt-subcategory-header {
    padding: 7px 12px 4px;
    font-size: 10px;
    font-weight: 700;
    color: #7fb6dd;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    cursor: default;
  }
  .pcr-prompt-dropdown-item:hover {
    background: rgba(79, 195, 247, 0.2);
    color: #4fc3f7;
  }
  .pcr-prompt-dropdown-item-del {
    color: #666;
    cursor: pointer;
    padding: 0 4px;
    font-size: 14px;
  }
  .pcr-prompt-dropdown-item-del:hover { color: #e44; }
  .pcr-prompt-del-badge {
    color: #666;
    margin-left: 4px;
    font-size: 13px;
  }
  .pcr-prompt-btn:hover .pcr-prompt-del-badge { color: #e44; }
</style>
