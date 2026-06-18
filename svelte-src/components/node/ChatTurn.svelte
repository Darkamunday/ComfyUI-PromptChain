<script>
  // ChatTurn — one row in the chat timeline. Either a YOU bubble or an
  // AI prose block; AI rows may carry a ProposalCard if the turn has a
  // tool_use attached. Hover actions: Copy (AI), Regenerate (last turn),
  // Edit-and-resend (user turns).

  import { tick } from "svelte";
  import ProposalCard from "./ProposalCard.svelte";

  let {
    turn,
    index = -1,
    isLast = false,
    busy = false,
    proposal = null,
    onAccept = () => {},
    onReject = () => {},
    onRegenerate = () => {},
    onEditResend = () => {},
  } = $props();

  // Minimal markdown renderer — narrow to what the chat agent
  // actually emits (bullet lists from list_model_styles, inline
  // **bold**). Escapes HTML first, then applies inline + block
  // transforms. No external dependency.
  function renderAssistantMarkdown(text) {
    if (!text) return "";
    let html = String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    const lines = html.split("\n");
    const out = [];
    let inList = false;
    for (const line of lines) {
      const trimmed = line.replace(/^\s+/, "");
      const isBullet = /^[-*]\s+/.test(trimmed);
      if (isBullet) {
        if (!inList) {
          out.push('<ul class="pcr-ai-chat-list">');
          inList = true;
        }
        out.push("<li>" + trimmed.replace(/^[-*]\s+/, "") + "</li>");
      } else {
        if (inList) {
          out.push("</ul>");
          inList = false;
        }
        out.push(line);
      }
    }
    if (inList) out.push("</ul>");
    return out.join("\n");
  }

  // Skip rendering an assistant turn that has neither visible text
  // nor a proposal card — happens when the agent's hop1 was a pure
  // tool_use block (e.g. list_model_styles, no proposal generated),
  // followed by hop2 with the actual narration as its own turn.
  // Without this guard the chat shows an empty "AI" label.
  let assistantHasContent = $derived(
    turn.role !== "assistant" ||
    (turn.text && turn.text.trim()) ||
    (turn.proposalId && proposal)
  );

  // ── Copy ───────────────────────────────────────────────────────
  let copied = $state(false);
  let copyResetTimer = null;
  async function copyText() {
    try {
      await navigator.clipboard.writeText(turn.text || "");
      copied = true;
      clearTimeout(copyResetTimer);
      copyResetTimer = setTimeout(() => { copied = false; }, 1500);
    } catch {
      // Clipboard blocked (insecure context / denied) — silent; the
      // text is still selectable in the bubble.
    }
  }

  // ── Edit-and-resend (user turns) ───────────────────────────────
  let editing = $state(false);
  let editText = $state("");
  let editEl = $state(null);

  async function startEdit() {
    if (busy) return;
    editText = turn.text || "";
    editing = true;
    await tick();
    editEl?.focus();
    if (editEl) {
      editEl.style.height = "auto";
      editEl.style.height = `${editEl.scrollHeight}px`;
      editEl.selectionStart = editEl.selectionEnd = editText.length;
    }
  }
  function cancelEdit() {
    editing = false;
    editText = "";
  }
  function saveEdit() {
    const next = editText.trim();
    if (!next) return;
    editing = false;
    onEditResend(index, next);
  }
  function onEditKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      saveEdit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancelEdit();
    }
  }
  function autoGrowEdit() {
    if (!editEl) return;
    editEl.style.height = "auto";
    editEl.style.height = `${editEl.scrollHeight}px`;
  }
</script>

{#if turn.role === "user"}
  <div class="pcr-ai-chat-turn pcr-ai-chat-turn--{turn.role}">
    <div class="pcr-ai-chat-role-label pcr-ai-chat-role-label--{turn.role}">YOU</div>
    {#if editing}
      <div class="pcr-ai-chat-edit">
        <textarea
          bind:this={editEl}
          bind:value={editText}
          class="pcr-ai-chat-edit-input"
          rows="1"
          oninput={autoGrowEdit}
          onkeydown={onEditKeydown}
        ></textarea>
        <div class="pcr-ai-chat-edit-actions">
          <button type="button" class="pcr-ai-turn-action" onclick={cancelEdit}>Cancel</button>
          <button type="button" class="pcr-ai-turn-action pcr-ai-turn-action--primary" onclick={saveEdit} disabled={!editText.trim()}>Resend</button>
        </div>
      </div>
    {:else}
      {#if turn.images?.length}
        <div class="pcr-ai-chat-turn-images">
          {#each turn.images as img (img.hash)}
            <img class="pcr-ai-chat-turn-thumb" src={img.url} alt="attached image" />
          {/each}
        </div>
      {/if}
      {#if turn.text}
        <div class="pcr-ai-chat-user-bubble">{turn.text}</div>
      {/if}
      {#if !busy && turn.text}
        <div class="pcr-ai-turn-actions">
          <button type="button" class="pcr-ai-turn-action" title="Edit and resend" onclick={startEdit}>Edit</button>
          {#if isLast}
            <button type="button" class="pcr-ai-turn-action" title="Regenerate" onclick={() => onRegenerate()}>Regenerate</button>
          {/if}
        </div>
      {/if}
    {/if}
  </div>
{:else if assistantHasContent}
  <div class="pcr-ai-chat-turn pcr-ai-chat-turn--{turn.role}">
    <div class="pcr-ai-chat-role-label pcr-ai-chat-role-label--{turn.role}">AI</div>
    {#if turn.text}
      <div class="pcr-ai-chat-assistant-prose">{@html renderAssistantMarkdown(turn.text)}</div>
    {/if}
    {#if turn.proposalId && proposal}
      <ProposalCard
        proposalId={turn.proposalId}
        {proposal}
        {onAccept}
        {onReject}
      />
    {/if}
    {#if !busy && (turn.text || isLast)}
      <div class="pcr-ai-turn-actions">
        {#if turn.text}
          <button type="button" class="pcr-ai-turn-action" title="Copy to clipboard" onclick={copyText}>{copied ? "Copied" : "Copy"}</button>
        {/if}
        {#if isLast}
          <button type="button" class="pcr-ai-turn-action" title="Regenerate" onclick={() => onRegenerate()}>Regenerate</button>
        {/if}
      </div>
    {/if}
  </div>
{/if}
