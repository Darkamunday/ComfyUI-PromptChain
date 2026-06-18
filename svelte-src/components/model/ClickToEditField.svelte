<script>
  // ClickToEditField — inline click-to-edit field with dropdown or free text.
  // For "select" type, shows a searchable popup list.
  // For "text" type, shows an inline input.

  let {
    label = "",
    value = "",
    type = "text",
    options = [],
    placeholder = "",
    onChange = () => {},
  } = $props();

  let currentVal = $state(value);
  let editing = $state(false);
  let searchQuery = $state("");
  let inputEl;

  $effect(() => { if (!editing) currentVal = value; });

  function displayLabel(v) {
    if (type === "select" && options) {
      const match = options.find(o => o.id === v);
      return match ? match.label : (v || "");
    }
    return v || "";
  }

  let display = $derived(displayLabel(currentVal));
  let isUnset = $derived(!display && !!placeholder);

  let filteredOptions = $derived.by(() => {
    if (type !== "select" || !options) return [];
    const q = searchQuery.toLowerCase();
    if (!q) return options;
    return options.filter(o => o.label.toLowerCase().includes(q));
  });

  function startEdit() {
    if (type === "select") {
      editing = !editing;
      searchQuery = "";
    } else {
      editing = true;
      searchQuery = currentVal;
    }
  }

  function pick(val) {
    const prev = currentVal;
    currentVal = val;
    editing = false;
    if (currentVal !== prev) onChange(currentVal);
  }

  function handleInputKeydown(e) {
    if (e.key === "Enter") {
      pick(searchQuery.trim());
    } else if (e.key === "Escape") {
      editing = false;
    }
  }
</script>

<span class="pcr-model-panel-field">
  <span class="pcr-model-panel-field-label">{label}:</span>
  <span class="pcr-model-panel-field-value"
    class:pcr-field-unset={isUnset}
    onclick={(e) => { e.stopPropagation(); startEdit(); }}>
    {isUnset ? placeholder : display}
  </span>

  {#if editing}
    {#if type === "select"}
      <div class="pcr-mode-menu" style="position:absolute;z-index:10000;min-width:120px">
        <div class="pcr-mode-menu-search-container">
          <input class="pcr-mode-menu-search" type="text"
            placeholder="Search..."
            bind:value={searchQuery}
            bind:this={inputEl}
            onkeydown={(e) => { if (e.key === "Escape") editing = false; }} />
        </div>
        <div class="pcr-mode-menu-separator"></div>
        <div class="pcr-mode-menu-list" style="max-height:200px;overflow-y:auto">
          {#each filteredOptions as opt}
            <div class="pcr-mode-menu-item"
              class:pcr-mode-menu-item-current={opt.id === currentVal}
              onclick={(e) => { e.stopPropagation(); pick(opt.id); }}>
              {opt.label}
            </div>
          {/each}
        </div>
      </div>
    {:else}
      <div class="pcr-mode-menu" style="position:absolute;z-index:10000;min-width:120px;padding:4px">
        <input class="pcr-mode-menu-search" type="text"
          bind:value={searchQuery}
          bind:this={inputEl}
          onkeydown={handleInputKeydown}
          placeholder={placeholder || `Type ${label.toLowerCase()}...`} />
      </div>
    {/if}
  {/if}
</span>

<style>
  .pcr-model-panel-field { display: inline-flex; align-items: baseline; gap: 4px; font-size: 12px; }
  .pcr-model-panel-field-label { color: #666; white-space: nowrap; }
  .pcr-model-panel-field-value {
    color: #bbb;
    cursor: pointer;
    border-bottom: 1px dashed transparent;
    transition: border-color 0.15s;
  }
  .pcr-model-panel-field-value:hover { border-bottom-color: #666; }
  .pcr-model-panel-field-value:empty::after { content: "..."; color: #555; }
  .pcr-field-unset { color: #c44; font-style: italic; }
</style>
