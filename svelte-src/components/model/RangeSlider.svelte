<script>
  // RangeSlider — dual-thumb slider for editing min/max range.

  let {
    min, max, step, rangeMin, rangeMax,
    onChange = () => {},
  } = $props();

  let lo = $state(rangeMin ?? min);
  let hi = $state(rangeMax ?? max);
  let activeThumb = $state(null);
  let trackEl;

  $effect(() => { lo = rangeMin ?? min; hi = rangeMax ?? max; });

  function toPercent(val) {
    return Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100));
  }

  function snapToStep(val) {
    return Math.round((val - min) / step) * step + min;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function formatVal(v) {
    return step >= 1 ? String(v) : v.toFixed(String(step).split(".")[1]?.length || 2);
  }

  let pctLo = $derived(toPercent(lo));
  let pctHi = $derived(toPercent(hi));

  function posToValue(clientX) {
    const rect = trackEl.getBoundingClientRect();
    const pct = clamp((clientX - rect.left) / rect.width, 0, 1);
    return snapToStep(min + pct * (max - min));
  }

  function update() {
    onChange(lo, hi);
  }

  function onPointerDown(e) {
    e.preventDefault();
    e.stopPropagation();
    trackEl.setPointerCapture(e.pointerId);
    const v = posToValue(e.clientX);
    activeThumb = Math.abs(v - lo) <= Math.abs(v - hi) ? "lo" : "hi";
    if (activeThumb === "lo") lo = clamp(snapToStep(v), min, hi);
    else hi = clamp(snapToStep(v), lo, max);
    update();
  }

  function onPointerMove(e) {
    if (!activeThumb) return;
    const v = posToValue(e.clientX);
    if (activeThumb === "lo") lo = clamp(snapToStep(v), min, hi);
    else hi = clamp(snapToStep(v), lo, max);
    update();
  }

  function onPointerUp() {
    activeThumb = null;
  }

  export function getRange() { return [lo, hi]; }
</script>

<div class="pcr-slider-container">
  <div class="pcr-slider-track" bind:this={trackEl}
    onpointerdown={onPointerDown} onpointermove={onPointerMove} onpointerup={onPointerUp}>
    <div class="pcr-slider-zone pcr-slider-zone-editable"
      style:left="{pctLo}%" style:width="{pctHi - pctLo}%"></div>
    <div class="pcr-slider-thumb pcr-thumb-blue" style:left="{pctLo}%"></div>
    <div class="pcr-slider-thumb pcr-thumb-blue" style:left="{pctHi}%"></div>
  </div>
  <input type="number" class="pcr-slider-input pcr-slider-input-half"
    {min} {max} {step} value={formatVal(lo)}
    onchange={(e) => { lo = clamp(snapToStep(parseFloat(e.target.value) || min), min, hi); update(); }} />
  <span class="pcr-slider-range-sep">&ndash;</span>
  <input type="number" class="pcr-slider-input pcr-slider-input-half"
    {min} {max} {step} value={formatVal(hi)}
    onchange={(e) => { hi = clamp(snapToStep(parseFloat(e.target.value) || max), lo, max); update(); }} />
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
  /* blue range zone on track */
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

  /* range slider inputs */
  .pcr-slider-input-half {
    width: 42px;
  }
  .pcr-slider-range-sep {
    color: #555;
    font-size: 12px;
    flex-shrink: 0;
  }
</style>
