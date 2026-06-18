<script>
  // HoverCard — thumbnail + meta popup on pointer hover.

  let {
    data = null,
    pickerEl = null,
  } = $props();

  let visible = $state(false);
  let style = $state("");

  export function show(anchorEl, info) {
    data = info;
    if (!info?.thumbnail || !pickerEl) return;
    visible = true;

    requestAnimationFrame(() => {
      const pickerRect = pickerEl.getBoundingClientRect();
      const cardWidth = 220;
      const rightSpace = window.innerWidth - pickerRect.right;
      const left = rightSpace > cardWidth + 10
        ? pickerRect.right + 6
        : pickerRect.left - cardWidth - 6;
      style = `left:${left}px;top:${pickerRect.top}px;width:${cardWidth}px`;
    });
  }

  export function hide() {
    visible = false;
    data = null;
  }
</script>

{#if visible && data?.thumbnail}
  <div class="pcr-hover-card" {style}>
    <img class="pcr-hover-card-img" src={data.thumbnail} alt=""
         onerror={(e) => { e.currentTarget.style.display = "none"; }} />
    <div class="pcr-hover-card-body">
      <div class="pcr-hover-card-name">{data.name || ""}</div>
      {#if data.version}
        <div class="pcr-hover-card-version">{data.version}</div>
      {/if}
      {#if data.base_model || data.thumbs_up || data.downloads}
        <div class="pcr-hover-card-meta">
          {[data.base_model, data.thumbs_up ? `👍 ${data.thumbs_up}` : "", data.downloads ? `↓ ${data.downloads}` : ""]
            .filter(Boolean).join(" · ")}
        </div>
      {/if}
      {#if data.tags?.length}
        <div class="pcr-hover-card-tags">{data.tags.slice(0, 5).join(", ")}</div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .pcr-hover-card {
    position: fixed;
    width: 220px;
    background: rgba(30, 30, 30, 0.98);
    backdrop-filter: blur(12px);
    border: 1px solid #444;
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.6);
    z-index: 100001;
    overflow: hidden;
    pointer-events: none;
  }
  .pcr-hover-card-img {
    width: 100%;
    aspect-ratio: 2 / 3;
    object-fit: cover;
    display: block;
    background: #222;
  }
  .pcr-hover-card-body {
    padding: 8px 10px;
  }
  .pcr-hover-card-name {
    font-size: 12px;
    font-weight: 600;
    color: #eee;
    margin-bottom: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pcr-hover-card-version {
    font-size: 10px;
    color: #999;
    margin-bottom: 4px;
  }
  .pcr-hover-card-meta {
    font-size: 10px;
    color: #777;
    line-height: 1.4;
  }
  .pcr-hover-card-tags {
    font-size: 9px;
    color: #666;
    margin-top: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
