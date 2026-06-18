<script>
  // ChatTimeline — purely presentational list of turns + an in-flight
  // indicator. Scrolling lives on the parent (.pcr-ai-panel-body) and is
  // handled by AIAssistant.svelte; the auto-scroll-on-new-content + pin-
  // tracking logic must run on the actual scroll container, which is the
  // body, not this list.

  import ChatTurn from "./ChatTurn.svelte";

  let {
    turns = [],
    proposals = {},
    busy = false,
    thinkingState = null,
    emptyHint = "",
    // While the parent is probing/recovering the provider (e.g. waking
    // Ollama), suppress the empty-state hint so the panel doesn't read
    // as "ready to use" alongside an in-progress banner.
    suppressEmptyHint = false,
    // True when the server has dropped older turns from the model's
    // context window — shown as a marker so the condensing is honest.
    condensed = false,
    onAccept = () => {},
    onReject = () => {},
    onRegenerate = () => {},
    onEditResend = () => {},
  } = $props();

  let elapsedLabel = $derived.by(() => {
    const e = thinkingState?.elapsed || 0;
    if (e <= 0) return "";
    if (e < 60) return `${e}s`;
    const m = Math.floor(e / 60);
    return m < 60 ? `${m}m ${e % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
  });
  let tokensLabel = $derived.by(() => {
    const t = thinkingState?.tokens || 0;
    if (t <= 0) return "";
    if (t < 1000) return `↓ ${t} tokens`;
    const k = t / 1000;
    return k < 10 ? `↓ ${k.toFixed(1)}k tokens` : `↓ ${Math.round(k)}k tokens`;
  });
</script>

<div class="pcr-ai-chat-timeline">
  {#if turns.length === 0 && !busy && emptyHint && !suppressEmptyHint}
    <div class="pcr-ai-chat-empty-hint">{emptyHint}</div>
  {/if}

  {#if condensed && turns.length > 0}
    <div class="pcr-ai-chat-condensed" title="To stay within the model's context window, older turns have been summarized for the assistant. The full conversation is still shown here.">
      ⋯ earlier turns condensed for the assistant's memory
    </div>
  {/if}

  {#each turns as turn, i (i)}
    <ChatTurn
      {turn}
      index={i}
      isLast={i === turns.length - 1}
      {busy}
      proposal={turn.proposalId ? proposals[turn.proposalId] : null}
      {onAccept}
      {onReject}
      {onRegenerate}
      {onEditResend}
    />
  {/each}

  {#if busy}
    <div class="pcr-ai-chat-thinking">
      <span class="pcr-ai-panel-status">
        {thinkingState?.status || "Thinking"}<span class="pcr-ai-panel-dots"><span>.</span><span>.</span><span>.</span></span>
      </span>
      {#if elapsedLabel || tokensLabel}
        <span class="pcr-ai-panel-elapsed">
          {#if elapsedLabel}{elapsedLabel}{/if}
          {#if elapsedLabel && tokensLabel} · {/if}
          {#if tokensLabel}{tokensLabel}{/if}
        </span>
      {/if}
    </div>
  {/if}
</div>
