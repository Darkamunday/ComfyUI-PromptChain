<script>
  // ProposalCard — durable artifact for one `apply_prompt_patch` tool call.
  // Rendered inside a chat turn; its lifecycle (pending → applied/rejected/
  // failed) is owned by the parent (AIAssistant.svelte). The diff body
  // reuses .pcr-ai-panel-diff-* classes so the visual stays consistent
  // with what the legacy single-shot path showed.

  let {
    proposalId,
    proposal,
    onAccept = () => {},
    onReject = () => {},
  } = $props();

  let status = $derived(proposal?.status || "pending");
  let sections = $derived(proposal?.tool_result?.sections || []);
  let timestamp = $derived(proposal?.appliedAt || proposal?.createdAt || Date.now());
  let toolRequest = $derived(proposal?.tool_input?.request || "");
  // Buttons appear on any pending proposal regardless of current mode.
  // Mode is a forward-looking setting for the next submit; if the user
  // switched the dropdown after submitting (race), an in-flight ask-mode
  // proposal would otherwise come back pending with no way to accept.
  let canAct = $derived(status === "pending");

  let statusLabel = $derived.by(() => {
    if (status === "applied") return "APPLIED";
    if (status === "rejected") return "REJECTED";
    if (status === "failed") return "FAILED";
    return "PROPOSAL";
  });

  // Relative timestamp ("2s ago", "1m ago", "3h ago", "2d ago"); reactive
  // so a card that says "just now" updates as time passes. Coarse precision
  // is fine — exact times aren't load-bearing.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 30_000);
    return () => clearInterval(id);
  });
  let relTime = $derived.by(() => {
    const diff = Math.max(0, Math.floor((now - timestamp) / 1000));
    if (diff < 60) return "";
    const m = Math.floor(diff / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  });
</script>

<div class="pcr-ai-proposal-card">
  <div class="pcr-ai-proposal-header">
    <span class="pcr-ai-proposal-pill pcr-ai-proposal-pill--{status}">{statusLabel}</span>
    {#if toolRequest}
      <span class="pcr-ai-proposal-request" title={toolRequest}>{toolRequest}</span>
    {/if}
    <span class="pcr-ai-proposal-time">{relTime}</span>
  </div>

  {#if status === "failed"}
    <div class="pcr-ai-proposal-error">
      {proposal?.tool_result?.error || "Patch failed"}
    </div>
  {:else if sections.length === 0}
    <div class="pcr-ai-proposal-empty">No diff sections returned.</div>
  {:else}
    <div class="pcr-ai-proposal-body">
      {#each sections as s}
        <div class="pcr-ai-panel-diff-section">
          <div class="pcr-ai-panel-diff-label">{s.header}</div>
          {#if s.body_text && !s.is_negative}
            <div class="pcr-ai-panel-diff-prose pcr-ai-panel-diff-prose--polarity-{s.is_negative ? 'neg' : 'pos'} pcr-ai-panel-diff-prose--action-{s.is_removal ? 'remove' : 'add'}">{s.body_text}</div>
          {:else}
            <div class="pcr-ai-panel-diff-chips">
              {#each s.tokens || [] as t}
                <span class="pcr-ai-panel-diff-chip pcr-ai-panel-diff-chip--polarity-{s.is_negative ? 'neg' : 'pos'} pcr-ai-panel-diff-chip--action-{s.is_removal ? 'remove' : 'add'}">{t}</span>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  {#if canAct}
    <div class="pcr-ai-panel-actions">
      <button
        class="pcr-ai-panel-action pcr-ai-panel-action--apply"
        type="button"
        onclick={() => onAccept(proposalId)}
      >Accept</button>
      <button
        class="pcr-ai-panel-action pcr-ai-panel-action--reject"
        type="button"
        onclick={() => onReject(proposalId)}
      >Reject</button>
    </div>
  {/if}
</div>
