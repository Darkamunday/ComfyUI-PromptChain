<script>
  // MultiSelectCombo — checkbox list with default indicator for combo range editing.

  let {
    options = [],
    selected: initialSelected = null,
    defaultVal: initialDefault = null,
    onChange = () => {},
  } = $props();

  let sel = $state(new Set(initialSelected || options));
  let def = $state(initialDefault || options[0]);

  function toggle(opt, checked) {
    // Immutable toggle: build the next Set in one step so Svelte sees a
    // fresh reference and there's no window of mutate-then-reassign.
    const next = new Set(sel);
    if (checked) next.add(opt); else next.delete(opt);
    sel = next;
    if (!sel.has(def)) def = [...sel][0] || options[0];
    onChange(def, [...sel]);
  }

  function setDefault(opt) {
    def = opt;
    onChange(def, [...sel]);
  }

  export function getDefault() { return def; }
  export function getSelected() { return [...sel]; }
</script>

<div class="pcr-multiselect-container">
  {#each options as opt}
    <label class="pcr-multiselect-row">
      <input type="checkbox" checked={sel.has(opt)}
        onchange={(e) => toggle(opt, e.target.checked)} />
      <span class="pcr-multiselect-name">{opt}</span>
      {#if sel.has(opt)}
        <span
          class="pcr-multiselect-default"
          class:pcr-multiselect-default-active={opt === def}
          title={opt === def ? "Default" : "Set as default"}
          onclick={(e) => { e.preventDefault(); e.stopPropagation(); setDefault(opt); }}>
          {opt === def ? "★" : "☆"}
        </span>
      {/if}
    </label>
  {/each}
</div>

<style>
  .pcr-multiselect-container {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
  }
  .pcr-multiselect-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 6px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 12px;
    color: #ccc;
  }
  .pcr-multiselect-row:hover { background: rgba(255, 255, 255, 0.05); }
  .pcr-multiselect-row input[type="checkbox"] { margin: 0; cursor: pointer; }
  .pcr-multiselect-name { flex: 1; }
  .pcr-multiselect-default {
    cursor: pointer;
    font-size: 14px;
    color: #555;
    transition: color 0.15s;
  }
  .pcr-multiselect-default:hover { color: #f0ad4e; }
  .pcr-multiselect-default-active { color: #f0ad4e; }
</style>
