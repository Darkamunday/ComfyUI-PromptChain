<script>
  let { logoTextUrl = "" } = $props();
</script>

<div class="pcr-fs-welcome">
  {#if logoTextUrl}
    <img src={logoTextUrl} draggable="false" alt="PromptChain" />
  {/if}
</div>

<style>
  .pcr-fs-welcome {
    display: flex;
    flex: 1;
    align-items: center;
    justify-content: center;
    min-height: 0;
    overflow: hidden;
  }
  .pcr-fs-welcome img {
    width: 360px;
    height: auto;
    opacity: 0.04;
    filter: invert(1);
    pointer-events: none;
  }
</style>
