<script>
  // SettingsSlider — single-thumb slider with saved-value marker,
  // range zone, optional resolution ticks, and color-coded state.

  let {
    min, max, step, value, savedValue = undefined, rangeMin = undefined,
    rangeMax = undefined, userSaved = false, ticks = null,
    rail = undefined, // optional CSS background for the track (e.g. temp gradient)
    onChange = () => {},
  } = $props();

  let currentVal = $state(value);
  let dragging = $state(false);
  let trackEl;

  $effect(() => { currentVal = value; });

  const hasSaved = $derived(savedValue !== undefined);
  const hasRange = $derived(rangeMin !== undefined && rangeMax !== undefined);
  const hasTicks = $derived(Array.isArray(ticks) && ticks.length > 0);

  function toPercent(val) {
    return Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100));
  }

  function snapToStep(val) {
    return Math.round((val - min) / step) * step + min;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function formatVal(v) {
    if (step >= 1) return String(v);
    // Derive decimal places from the step's magnitude so scientific
    // notation (e.g. step=1e-3) and float-precision noise don't break
    // the string-split heuristic used previously.
    const decimals = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step))));
    return v.toFixed(decimals);
  }

  let fillPct = $derived(toPercent(currentVal));
  let thumbColor = $derived.by(() => {
    const atSaved = hasSaved && Math.abs(currentVal - savedValue) < (step || 0.001) * 0.5;
    const inRange = hasRange && currentVal >= rangeMin && currentVal <= rangeMax;
    return atSaved ? "green" : inRange ? "blue" : "gray";
  });
  let inputColor = $derived(
    thumbColor === "green" ? "#5ed357" : thumbColor === "blue" ? "#5dcaff" : ""
  );

  function setValue(v) {
    v = snapToStep(clamp(v, min, max));
    if (v === currentVal) return;
    currentVal = v;
    onChange(v);
  }

  function posToValue(clientX) {
    const rect = trackEl.getBoundingClientRect();
    const pct = clamp((clientX - rect.left) / rect.width, 0, 1);
    return snapToStep(min + pct * (max - min));
  }

  function onPointerDown(e) {
    e.preventDefault();
    e.stopPropagation();
    dragging = true;
    trackEl.setPointerCapture(e.pointerId);
    setValue(posToValue(e.clientX));
  }

  function onPointerMove(e) {
    if (dragging) setValue(posToValue(e.clientX));
  }

  function onPointerUp() {
    dragging = false;
  }

  function onTickClick(tickValue, e) {
    e.stopPropagation();
    e.preventDefault();
    setValue(snapToStep(tickValue));
  }

  function onInputChange(e) {
    setValue(parseFloat(e.target.value) || min);
  }

  function onInputKeydown(e) {
    if (e.key === "Escape") {
      e.target.value = formatVal(currentVal);
      e.target.blur();
    }
  }
</script>

<div class="pcr-slider-container">
  {#if hasTicks}
    <div class="pcr-slider-track-wrapper">
      <div class="pcr-slider-track" bind:this={trackEl} style:background={rail}
        onpointerdown={onPointerDown} onpointermove={onPointerMove} onpointerup={onPointerUp}>
        {#if hasRange}
          <div class="pcr-slider-zone"
            style:left="{toPercent(rangeMin)}%"
            style:width="{toPercent(rangeMax) - toPercent(rangeMin)}%"></div>
        {/if}
        {#if hasSaved}
          <div class="pcr-slider-marker"
            title="{userSaved ? 'Saved' : 'Default'}: {savedValue}"
            style:left="clamp(7px, {toPercent(savedValue)}%, calc(100% - 7px))"></div>
        {/if}
        <div class="pcr-slider-fill" style:width="{fillPct}%"></div>
        <div class="pcr-slider-thumb pcr-thumb-{thumbColor}"
          class:pcr-thumb-dragging={dragging}
          style:left="clamp(7px, {fillPct}%, calc(100% - 7px))"></div>
      </div>
      <div class="pcr-slider-ticks">
        {#each ticks as tick}
          <div class="pcr-slider-tick"
            class:pcr-tick-active={Math.abs(tick.value - currentVal) < step * 1.5}
            style:left="{toPercent(tick.value)}%"
            onpointerdown={(e) => onTickClick(tick.value, e)}>
            <span class="pcr-slider-tick-label">{tick.label}</span>
          </div>
        {/each}
      </div>
    </div>
  {:else}
    <div class="pcr-slider-track" bind:this={trackEl} style:background={rail}
      onpointerdown={onPointerDown} onpointermove={onPointerMove} onpointerup={onPointerUp}>
      {#if hasRange}
        <div class="pcr-slider-zone"
          style:left="{toPercent(rangeMin)}%"
          style:width="{toPercent(rangeMax) - toPercent(rangeMin)}%"></div>
      {/if}
      {#if hasSaved}
        <div class="pcr-slider-marker"
          title="{userSaved ? 'Saved' : 'Default'}: {savedValue}"
          style:left="clamp(7px, {toPercent(savedValue)}%, calc(100% - 7px))"></div>
      {/if}
      <div class="pcr-slider-fill" style:width="{fillPct}%"></div>
      <!-- clamp keeps the thumb inside the track ends (native-input behavior)
           instead of overhanging into neighboring layout at min/max -->
      <div class="pcr-slider-thumb pcr-thumb-{thumbColor}"
        class:pcr-thumb-dragging={dragging}
        style:left="clamp(7px, {fillPct}%, calc(100% - 7px))"></div>
    </div>
  {/if}
  <input type="number" class="pcr-slider-input"
    {min} {max} {step}
    value={formatVal(currentVal)}
    style:color={inputColor}
    onchange={onInputChange}
    onkeydown={onInputKeydown} />
</div>

<style>
  .pcr-slider-container {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
  }
  .pcr-slider-track {
    position: relative;
    flex: 1;
    height: 8px;
    background: #3a3a3a;
    border-radius: 4px;
    cursor: pointer;
    touch-action: none;
  }
  .pcr-slider-fill {
    display: none;
  }
  .pcr-slider-thumb {
    position: absolute;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid #fff;
    top: 50%;
    transform: translate(-50%, -50%);
    cursor: grab;
    z-index: 2;
    transition: box-shadow 0.1s;
  }
  .pcr-slider-thumb.pcr-thumb-dragging { cursor: grabbing; }
  .pcr-slider-thumb.pcr-thumb-gray { background: #888; }
  .pcr-slider-thumb.pcr-thumb-green {
    background: #5ed357;
    box-shadow: 0 0 6px rgba(94, 211, 87, 0.4);
  }
  .pcr-slider-marker {
    position: absolute;
    width: 3px;
    height: 14px;
    background: rgba(94, 211, 87, 0.8);
    border-radius: 1px;
    top: 50%;
    transform: translate(-50%, -50%);
    pointer-events: none;
    z-index: 1;
  }
  .pcr-slider-input {
    width: 54px;
    background: #2a2a2a;
    border: 1px solid #444;
    border-radius: 4px;
    color: #ddd;
    padding: 3px 5px;
    font-size: 12px;
    font-family: monospace;
    text-align: right;
  }
  .pcr-slider-input:focus {
    border-color: #4fc3f7;
    outline: none;
  }
  .pcr-slider-track-wrapper {
    flex: 1;
    min-width: 0;
  }
  .pcr-slider-ticks {
    position: relative;
    width: 100%;
    height: 16px;
    margin-top: -4px;
  }
  .pcr-slider-tick {
    position: absolute;
    transform: translateX(-50%);
    cursor: pointer;
    padding: 2px 0;
  }
  .pcr-slider-tick::before {
    content: "";
    display: block;
    width: 1px;
    height: 5px;
    background: #555;
    margin: 0 auto 1px;
  }
  .pcr-slider-tick-label {
    font-size: 9px;
    color: #666;
    pointer-events: none;
    white-space: nowrap;
  }
  .pcr-slider-tick:hover .pcr-slider-tick-label {
    color: #aaa;
  }
  .pcr-slider-tick.pcr-tick-active .pcr-slider-tick-label {
    color: #4fc3f7;
    font-weight: bold;
  }
  .pcr-slider-tick.pcr-tick-active::before {
    background: #4fc3f7;
  }

  /* range zone on track */
  .pcr-slider-zone {
    position: absolute;
    top: 0;
    height: 100%;
    background: rgb(62 144 214 / 64%);
    border-radius: 4px;
    pointer-events: none;
  }
  .pcr-slider-zone-editable {
    background: rgba(79, 195, 247, 0.4);
  }
  .pcr-slider-thumb.pcr-thumb-blue {
    background: #4fc3f7;
    box-shadow: 0 0 4px rgba(79, 195, 247, 0.4);
  }
</style>
