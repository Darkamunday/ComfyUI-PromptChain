<script>
  // SavePathInput — Explorer-style save-path field. Committed folder segments
  // render as clickable chips (click = jump back to that level), the tail
  // stays a free-text input (usually the filename pattern), and typing
  // surfaces matching EXISTING output folders in a dropdown. Typing "/"
  // commits the text before it as a new segment; Backspace at the start of
  // the input pops the last chip back into editable text.

  let { value = "", onChange = () => {}, fetchApi = null } = $props();

  let inputEl;
  let editEl;
  let focused = $state(false);
  let editMode = $state(false); // Explorer's address bar: breadcrumbs <-> raw text
  let ddIdx = $state(-1);
  let folderCache = $state({}); // chipsPath -> string[] of existing subfolders

  function enterEditMode(selectAll) {
    editMode = true;
    requestAnimationFrame(() => {
      editEl?.focus();
      if (selectAll) editEl?.select();
      else editEl?.setSelectionRange(value.length, value.length);
    });
  }

  let chips = $derived(value.includes("/") ? value.split("/").slice(0, -1) : []);
  let tail = $derived(value.includes("/") ? value.split("/").pop() : value);
  let chipsPath = $derived(chips.join("/"));

  // Folder listing for the current level, cached per path. A path containing
  // not-yet-created folders just yields no suggestions.
  $effect(() => {
    const p = chipsPath;
    if (!fetchApi || folderCache[p] !== undefined) return;
    fetchApi(`/promptchain/browse?scope=output&path=${encodeURIComponent(p)}&sort=name&direction=asc`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const folders = (data?.items || []).filter((i) => i.type === "folder").map((i) => i.name);
        folderCache = { ...folderCache, [p]: folders };
      })
      .catch(() => { folderCache = { ...folderCache, [p]: [] }; });
  });

  let matches = $derived((() => {
    if (!focused) return [];
    const list = folderCache[chipsPath] || [];
    const t = tail.trim().toLowerCase();
    if (!t) return list;
    return list.filter((n) => n.toLowerCase().startsWith(t) && n.toLowerCase() !== t);
  })());

  function emit(newChips, newTail) {
    onChange(newChips.length ? `${newChips.join("/")}/${newTail}` : newTail);
  }

  function caretTo(pos) {
    requestAnimationFrame(() => inputEl?.setSelectionRange(pos, pos));
  }

  function handleInput(e) {
    let text = e.target.value;
    ddIdx = -1;
    const slash = text.indexOf("/");
    if (slash >= 0) {
      const seg = text.slice(0, slash).trim();
      const rest = text.slice(slash + 1);
      emit(seg ? [...chips, seg] : chips, rest);
      // keep the caret where the user was typing, now in tail coordinates
      caretTo(Math.max(0, Math.min(rest.length, (e.target.selectionStart ?? rest.length) - slash - 1)));
      return;
    }
    emit(chips, text);
  }

  function handleKeydown(e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
      // Select-all means the WHOLE path, not just the tail — flip to raw text.
      e.preventDefault();
      enterEditMode(true);
      return;
    }
    // Selecting leftward past the tail's start continues into the folder
    // region: flip to raw text, carrying the selection over (backward-anchored
    // so further Shift+Lefts keep extending).
    if (e.shiftKey && (e.key === "ArrowLeft" || e.key === "Home") && chips.length
        && (e.key === "Home" || inputEl?.selectionStart === 0)) {
      e.preventDefault();
      const prefixLen = value.length - tail.length; // chips + trailing slash
      const selEnd = prefixLen + (inputEl?.selectionEnd ?? 0);
      const selStart = e.key === "Home" ? 0 : Math.max(0, prefixLen - 1);
      editMode = true;
      requestAnimationFrame(() => {
        editEl?.focus();
        editEl?.setSelectionRange(selStart, selEnd, "backward");
      });
      return;
    }
    if (e.key === "Backspace" && inputEl?.selectionStart === 0 && inputEl?.selectionEnd === 0 && chips.length) {
      e.preventDefault();
      const popped = chips[chips.length - 1];
      emit(chips.slice(0, -1), popped + tail);
      caretTo(popped.length);
      return;
    }
    if (matches.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); ddIdx = (ddIdx + 1) % matches.length; return; }
      if (e.key === "ArrowUp") { e.preventDefault(); ddIdx = ddIdx <= 0 ? matches.length - 1 : ddIdx - 1; return; }
      if (e.key === "Enter" && ddIdx >= 0) { e.preventDefault(); e.stopPropagation(); pick(matches[ddIdx]); return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); ddIdx = -1; focused = false; return; }
    }
  }

  function pick(folder) {
    emit([...chips, folder], "");
    ddIdx = -1;
    if (editMode) {
      requestAnimationFrame(() => {
        editEl?.focus();
        const len = editEl?.value.length ?? 0;
        editEl?.setSelectionRange(len, len);
      });
    } else {
      inputEl?.focus();
    }
  }

  // Click in the tail text = switch to raw mode with the caret (or the
  // click-made selection) carried over at the same spot in the full string.
  function tailClickToEdit(e) {
    const prefixLen = value.length - tail.length;
    const start = prefixLen + (e.target.selectionStart ?? tail.length);
    const end = prefixLen + (e.target.selectionEnd ?? tail.length);
    editMode = true;
    requestAnimationFrame(() => {
      editEl?.focus();
      editEl?.setSelectionRange(start, end);
    });
  }

  // Explorer reveals open on the SERVER's desktop — only meaningful when this
  // browser is the server machine.
  const isLocalClient = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(window.location.hostname);

  function chipExists(i) {
    return (folderCache[chips.slice(0, i).join("/")] || []).includes(chips[i]);
  }

  function clickChip(i) {
    // Explorer semantics: clicking a real folder opens it in the OS file
    // manager. Not-yet-created folders have nothing to open.
    if (!isLocalClient || !fetchApi || !chipExists(i)) return;
    const rel = chips.slice(0, i + 1).join("/");
    fetchApi(`/promptchain/reveal-file?scope=output&path=${encodeURIComponent(rel)}`).catch(() => {});
  }
</script>

<div class="pcr-spi" class:focused>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="pcr-spi-field"
    onclick={(e) => { if (e.target === e.currentTarget) enterEditMode(false); }}
  >
    {#if editMode}
      <input
        bind:this={editEl}
        class="pcr-spi-edit"
        type="text"
        value={value}
        spellcheck="false"
        oninput={(e) => { ddIdx = -1; onChange(e.target.value); }}
        onkeydown={(e) => {
          if (matches.length) {
            if (e.key === "ArrowDown") { e.preventDefault(); ddIdx = (ddIdx + 1) % matches.length; return; }
            if (e.key === "ArrowUp") { e.preventDefault(); ddIdx = ddIdx <= 0 ? matches.length - 1 : ddIdx - 1; return; }
            if (e.key === "Enter" && ddIdx >= 0) { e.preventDefault(); e.stopPropagation(); pick(matches[ddIdx]); return; }
          }
          if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); e.stopPropagation(); editMode = false; }
        }}
        onfocus={() => { focused = true; }}
        onblur={() => { focused = false; editMode = false; ddIdx = -1; }}
      />
    {:else}
    {#each chips as chip, i}
      <button
        type="button"
        class="pcr-spi-chip"
        class:exists={(folderCache[chips.slice(0, i).join("/")] || []).includes(chip)}
        title={(folderCache[chips.slice(0, i).join("/")] || []).includes(chip) ? "Open this folder in Explorer" : "New folder (created on save)"}
        onclick={(e) => { e.stopPropagation(); clickChip(i); }}
      >{chip}</button>
      <span class="pcr-spi-sep">/</span>
    {/each}
    <input
      bind:this={inputEl}
      class="pcr-spi-input"
      type="text"
      value={tail}
      spellcheck="false"
      oninput={handleInput}
      onkeydown={handleKeydown}
      onclick={tailClickToEdit}
      onfocus={() => { focused = true; }}
      onblur={() => { focused = false; ddIdx = -1; }}
    />
    {/if}
  </div>
  {#if focused && matches.length}
    <div class="pcr-spi-dd">
      {#each matches as m, i}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class="pcr-spi-dd-item"
          class:active={i === ddIdx}
          onmousedown={(e) => { e.preventDefault(); pick(m); }}
        >
          <svg width="12" height="12" viewBox="0 -960 960 960" fill="currentColor"><path d="M160-160q-33 0-56.5-23.5T80-240v-480q0-33 23.5-56.5T160-800h240l80 80h320q33 0 56.5 23.5T880-640v400q0 33-23.5 56.5T800-160H160Z"/></svg>
          {m}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .pcr-spi { position: relative; width: 100%; }
  .pcr-spi-field {
    display: flex; align-items: center; flex-wrap: wrap; gap: 2px;
    width: 100%; box-sizing: border-box;
    padding: 4px 9px; min-height: 30px;
    background: #1c1c1c; border: 1px solid #3a3a3a; border-radius: 5px;
    cursor: text;
  }
  .pcr-spi.focused .pcr-spi-field { border-color: #c85909; }
  .pcr-spi-chip {
    padding: 1px 6px; font-size: 11.5px; line-height: 1.4;
    color: #bbb; background: #2c2c2c;
    border: 1px solid #3f3f3f; border-radius: 4px;
    cursor: pointer; white-space: nowrap;
  }
  .pcr-spi-chip:hover { color: #fff; border-color: #c85909; background: rgba(200, 89, 9, 0.15); }
  .pcr-spi-chip.exists { color: #9fd3ff; border-color: rgba(93, 202, 255, 0.35); }
  .pcr-spi-chip.exists:hover { color: #fff; border-color: #c85909; }
  .pcr-spi-sep { color: #555; font-size: 12px; }
  .pcr-spi-input {
    flex: 1; min-width: 80px;
    padding: 2px 0; font-size: 12px; color: #ddd;
    background: transparent; border: none; outline: none;
  }
  .pcr-spi-edit {
    flex: 1; width: 100%;
    padding: 2px 0; font-size: 12px; color: #ddd;
    background: transparent; border: none; outline: none;
  }
  .pcr-spi-dd {
    position: absolute; top: 100%; left: 0; right: 0; z-index: 10;
    margin-top: 3px; max-height: 180px; overflow-y: auto;
    background: #222; border: 1px solid #3a3a3a; border-radius: 5px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
  }
  .pcr-spi-dd-item {
    display: flex; align-items: center; gap: 7px;
    padding: 5px 10px; font-size: 12px; color: #bbb;
    cursor: pointer;
  }
  .pcr-spi-dd-item svg { color: #c8a35a; flex: none; }
  .pcr-spi-dd-item:hover, .pcr-spi-dd-item.active { background: rgba(200, 89, 9, 0.18); color: #fff; }
</style>
