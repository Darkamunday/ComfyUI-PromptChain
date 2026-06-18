<script>
  // AIAssistant — chat-style assistant panel docked beside the editor.
  // Wraps the existing /ai/patch flow as the apply_prompt_patch tool of a
  // Claude-driven chat agent (/promptchain/ai/chat). Per-node persistent
  // chat lives on node.properties.pcrAiChat / pcrAiProposals.
  //
  // Three modes (composer dropdown, persisted on node.properties.pcrAiAutoMode):
  //   ask       — propose, wait for Accept/Reject
  //   auto      — apply each tool result immediately (read-only proposal card)
  //   auto-run  — apply + queue prompt (toast on queue failure)

  import { onMount, onDestroy, tick, untrack } from "svelte";
  import { api } from "/scripts/api.js";
  import { app } from "/scripts/app.js";
  import { useApi } from "../../lib/api-context.js";
  import {
    readNodePrompt,
    isStandaloneMainPromptChain,
    extractNGrams,
    matchCharactersInDb,
    applyPromptText,
    cryptoId,
  } from "../../lib/ai-patch-helpers.js";

  import ChatTimeline from "./ChatTimeline.svelte";
  import ModeDropdown from "./ModeDropdown.svelte";
  import CommandsMenu from "./CommandsMenu.svelte";

  const DEFAULT_WIDTH = 320;
  const MIN_WIDTH = 200;

  let {
    node,
    shared,
    onToggle = () => {},
    onRegister = null,
  } = $props();

  const { getCanvasScale } = useApi();

  let panelEl;
  let dividerEl;
  let inputEl;
  let bodyEl;
  // Auto-scroll-to-bottom on new turn / streaming status — but only when
  // the user is already pinned to the bottom (within 32px). If they've
  // scrolled up to re-read past content, we leave them alone.
  let pinnedToBottom = $state(true);
  function recordPinState() {
    if (!bodyEl) return;
    const remaining = bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight;
    pinnedToBottom = remaining < 32;
  }
  // Experimental gate: the assistant only exists when the user opted in via
  // Settings > PromptChain > AI Assistant (experimental). A saved workflow
  // with the panel open must NOT resurrect it while the gate is off — the
  // menubar button would be hidden, leaving no way to close it.
  function aiAssistantEnabled() {
    return app.ui?.settings?.getSettingValue?.("PromptChain.AIAssistantEnabled") === true;
  }

  let isVisible = $state(!!node.properties?.pcrAiAssistant && aiAssistantEnabled());
  let panelWidth = $state(node.properties?.pcrAiPanelWidth ?? DEFAULT_WIDTH);
  let inputText = $state("");

  // Images staged for the next submit: {hash, url, name}. Uploaded to the
  // user folder immediately on attach (never base64 in chat state); only
  // the hash rides the /ai/chat request. Cleared after each send.
  let attachedImages = $state([]);
  // Images held alongside a queued message (submit while busy / rendering).
  let queuedImages = $state([]);
  let uploadingImage = $state(false);
  // Whether the configured model can see images — gates the attach button.
  // Defaults true; the probe narrows it.
  let visionCapable = $state(true);

  // ── Chat state ─────────────────────────────────────────────────
  // chat[]            — display turns: {role, text, proposalId?, timestamp}
  // proposals{}       — keyed by tool_use id: {tool_input, tool_result, status, createdAt, appliedAt?}
  // historyRaw[]      — canonical Anthropic messages (system|user|assistant blocks);
  //                     ferried back to /ai/chat unchanged so Claude sees its prior tool_use blocks
  // mode              — "ask" | "auto" | "auto-run"
  // busy              — in-flight; refuses new submits. NOT persisted (a crash mid-stream
  //                     would brick the panel until reload).
  let chat = $state(node.properties?.pcrAiChat ?? []);
  let proposals = $state(node.properties?.pcrAiProposals ?? {});
  let historyRaw = $state(node.properties?.pcrAiChatRaw ?? []);
  // Running mechanical recap of turns the server has dropped from the
  // model's context window (see _compact_history). Ferried back each turn
  // so the collapse is cumulative; non-empty drives the "condensed" marker.
  let chatSummary = $state(node.properties?.pcrAiChatSummary ?? "");
  let mode = $state(node.properties?.pcrAiAutoMode ?? "ask");
  // extraVerbs gates the macro-verb prompt block (Expand / Vary /
  // Condense / Reword / Enrich) server-side.
  let extraVerbs = $state(node.properties?.pcrAiExtraVerbs ?? true);
  let busy = $state(false);
  // Tracks the in-flight chat request_id so the stop button can hit
  // /promptchain/ai/cancel. Empty when no request is in flight.
  let activeRequestId = $state("");
  // When the user submits while busy (chat running) or while ComfyUI
  // is rendering in auto-run mode, the message is queued here instead
  // of dropped. Drained when the in-flight work completes.
  let queuedMessage = $state("");
  // True for one-shot when the user clicked the stop button — keeps
  // the catch path from surfacing the resulting fetch error as a banner.
  let stoppedByUser = false;
  // Verbatim text of the in-flight chat message. Used by the
  // render-yield handler to re-queue this same message when a
  // ComfyUI render starts mid-chat (VRAM contention).
  let activeMessageText = "";
  // True while /ai/chat is awaiting a response (LLM streaming).
  // Goes false the moment the response arrives, even before proposal
  // application finishes — so the render-yield only cancels real
  // LLM work, not the synchronous apply step that follows.
  let llmCallInFlight = false;
  let errorBanner = $state("");
  // Non-error transient status (e.g. "Ollama down, attempting start...").
  // Rendered in an amber-not-red banner so the user doesn't read it as a
  // hard failure when it's an in-progress recovery.
  let infoBanner = $state("");
  // True while we're checking provider availability / waking Ollama.
  // Suppresses the empty-state hint so the panel doesn't read as
  // "ready to use" while a recovery banner is also showing.
  let probing = $state(false);

  // Live stream status — updates from agent_text/tool_call/status WS events
  // while a request is in flight.
  let thinkingState = $state(null);
  let elapsedTimer = null;
  let elapsedStart = 0;
  let wsUnsub = null;

  // Submit-button enablement. While busy the button morphs into a stop
  // control (always clickable), so canSubmit only governs the "send"
  // pathway. An attached image alone is a valid submit (reverse-prompt),
  // so either non-empty text OR a staged image enables it.
  let canSubmit = $derived(
    (inputText.trim().length > 0 || attachedImages.length > 0) && !busy
  );

  // ── Persistence ────────────────────────────────────────────────
  // Mirror chat-state changes back to the node so they survive workflow
  // save/load. busy is intentionally NOT persisted.
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiChat = chat;
  });
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiProposals = proposals;
  });
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiChatRaw = historyRaw;
  });
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiChatSummary = chatSummary;
  });
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiAutoMode = mode;
  });
  $effect(() => {
    if (!node.properties) return;
    node.properties.pcrAiExtraVerbs = extraVerbs;
  });

  // Auto-scroll on new content. Tracks chat length, busy, thinkingState,
  // proposals so any chat-stream change retriggers it. Uses untrack on
  // pinnedToBottom so reading the pin state doesn't self-subscribe.
  $effect(() => {
    void chat.length;
    void busy;
    void thinkingState;
    void proposals;
    if (!bodyEl) return;
    if (!untrack(() => pinnedToBottom)) return;
    tick().then(() => {
      if (bodyEl) bodyEl.scrollTop = bodyEl.scrollHeight;
    });
  });

  // Grow textarea to fit content, capped at CSS max-height. Border-box
  // adjusted to avoid the 2px scrollbar flicker.
  const INPUT_MAX_H = 160;
  function autoGrowInput() {
    if (!inputEl) return;
    inputEl.style.height = "auto";
    inputEl.style.overflowY = "hidden";
    const borderY = inputEl.offsetHeight - inputEl.clientHeight;
    const target = inputEl.scrollHeight + borderY;
    if (target >= INPUT_MAX_H) {
      inputEl.style.height = `${INPUT_MAX_H}px`;
      inputEl.style.overflowY = "auto";
    } else {
      inputEl.style.height = `${target}px`;
    }
  }

  // ── Workflow queue helpers ──────────────────────────────────────

  async function isQueueBusy() {
    try {
      const q = await api.getQueue();
      const running = q?.Running?.length || 0;
      const pending = q?.Pending?.length || 0;
      return running + pending > 0;
    } catch {
      return false;
    }
  }

  function showToast(severity, summary, detail) {
    if (app?.extensionManager?.toast?.add) {
      app.extensionManager.toast.add({ severity, summary, detail, life: 4000 });
    } else {
      console.warn(`[PromptChain AI] ${summary}: ${detail}`);
    }
  }

  // ── Stream subscription ─────────────────────────────────────────

  function subscribeStream(reqId) {
    elapsedStart = Date.now();
    thinkingState = { status: "Thinking", elapsed: 0, tokens: 0 };

    const handler = (e) => {
      const data = e.detail || {};
      // Match the chat reqId AND any patch sub-call (`<reqId>-patch-XXXX`)
      // so the Qwen patch flow's status events surface live too.
      const did = data.request_id || "";
      if (did !== reqId && !did.startsWith(`${reqId}-patch-`)) return;

      switch (data.event) {
        case "thinking":
          thinkingState = {
            ...thinkingState,
            tokens: typeof data.tokens === "number"
              ? data.tokens
              : (thinkingState?.tokens || 0) + 1,
          };
          break;
        case "status":
          thinkingState = { ...thinkingState, status: data.content || "Thinking" };
          // Reset elapsed when leaving "Loading model" — VRAM populate
          // shouldn't read as model latency.
          if (data.content && data.content !== "Loading model"
              && thinkingState?._loading) {
            elapsedStart = Date.now();
            thinkingState.elapsed = 0;
          }
          thinkingState._loading = data.content === "Loading model";
          break;
        case "tool_call":
          thinkingState = { ...thinkingState, status: `Calling ${data.content || "tool"}` };
          break;
        case "agent_tool_call":
          thinkingState = { ...thinkingState, status: "Patching prompt" };
          break;
        case "agent_tool_result":
          thinkingState = { ...thinkingState, status: "Finishing up" };
          break;
        case "error":
          // Server-side error event; the POST result will also surface it,
          // so we let the response handler set the banner.
          break;
      }
    };
    api.addEventListener("promptchain_ai_stream", handler);
    wsUnsub = () => api.removeEventListener("promptchain_ai_stream", handler);

    elapsedTimer = setInterval(() => {
      if (thinkingState?._loading) return;
      thinkingState = {
        ...thinkingState,
        elapsed: Math.floor((Date.now() - elapsedStart) / 1000),
      };
    }, 1000);
  }

  function teardownStream() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
    wsUnsub?.();
    wsUnsub = null;
    thinkingState = null;
  }

  // ── Submit ─────────────────────────────────────────────────────

  // Top-level entry point from Enter / send button. Decides whether to
  // submit immediately or queue. Queueing happens when:
  //   - chat is already busy with an in-flight LLM request, OR
  //   - ComfyUI is currently rendering (VRAM contention: the chat
  //     LLM and the image model both want the GPU)
  // The queued message fires when the in-flight thing finishes (chat
  // drain in finally, comfy drain on "status" event with queue_remaining=0).
  async function handleSubmitOrQueue() {
    const userText = inputText.trim();
    if (!userText && attachedImages.length === 0) return;
    if (busy || await isQueueBusy()) {
      queuedMessage = userText;
      queuedImages = attachedImages;
      attachedImages = [];
      inputText = "";
      queueMicrotask(autoGrowInput);
      return;
    }
    await handleSubmit();
  }

  // Click handler for the stop button (button only renders in stop mode
  // when busy). Hits the backend cancel route — the active task is
  // cancelled and the existing fetch will reject; the catch path
  // rolls back the user's turn without surfacing an error banner.
  async function handleStop() {
    if (!activeRequestId) return;
    stoppedByUser = true;
    try {
      await fetch("/promptchain/ai/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: activeRequestId }),
      });
    } catch {
      // Best-effort: if cancel POST fails the in-flight task may still
      // complete server-side, but the user's intent was to stop — let
      // the eventual response drop the assistant turn anyway via the
      // stoppedByUser flag.
    }
  }

  // Render-yield: ComfyUI just started executing a prompt. If our chat
  // LLM is mid-stream, both are now fighting for VRAM — cancel chat
  // and re-queue its in-flight message so it fires when the render
  // finishes. llmCallInFlight (not busy) is the right gate because
  // busy stays true through synchronous proposal application after the
  // LLM has already released VRAM.
  function handleComfyExecutionStart() {
    if (!llmCallInFlight) return;
    if (!activeRequestId) return;
    if (activeMessageText) {
      queuedMessage = activeMessageText;
    }
    stoppedByUser = true;
    const idForCancel = activeRequestId;
    fetch("/promptchain/ai/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: idForCancel }),
    }).catch(() => {});
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    const userText = inputText.trim();
    // Snapshot staged images for THIS submit, then clear the tray so a
    // follow-up doesn't accidentally re-send them.
    const turnImages = attachedImages;
    attachedImages = [];
    if (!userText && turnImages.length === 0) return;

    const nodePrompt = readNodePrompt(node);
    const reqId = cryptoId();
    activeRequestId = reqId;
    activeMessageText = userText;
    llmCallInFlight = true;
    busy = true;
    errorBanner = "";

    // Append the user's turn locally so the UI feels responsive. The
    // turnId lets rollback survive Svelte re-runs that replace the
    // chat array — object-identity matching breaks if the array is
    // rebuilt from persistence between push and rollback.
    const now = Date.now();
    const turnId = cryptoId();
    const userTurn = { role: "user", text: userText, timestamp: now, _turnId: turnId };
    if (turnImages.length) {
      // Thumbnails for display only — {hash,url}. Persisted with the turn.
      userTurn.images = turnImages.map((i) => ({ hash: i.hash, url: i.url }));
    }
    chat = [...chat, userTurn];
    inputText = "";
    queueMicrotask(autoGrowInput);

    subscribeStream(reqId);

    try {
      // Bios preflight (matches the legacy single-shot path so /ai/patch
      // sees identical inputs whether called directly or via the agent).
      // Limit nodePrompt n-grams to header lines (// Character: ...,
      // // Outfit: ...) — bio prose body words ("Tony Stark from Marvel",
      // "Iron Man powered armor", "boot thrusters with jet") otherwise
      // generate junk single-word grams that stage-3 prefix-match against
      // unrelated DB chars (powered_ciel, jet_black, still_in_love_...),
      // loading 5 phantom bios and breaking downstream rendering.
      const nodePromptHeaders = (nodePrompt || "")
        .split("\n")
        .filter((l) => l.trim().startsWith("//"))
        .join("\n");
      const ngrams = Array.from(new Set([
        ...extractNGrams(userText),
        ...extractNGrams(nodePromptHeaders),
      ]));
      const bios = await matchCharactersInDb(ngrams, userText, nodePrompt);

      const modelInfo = node?._pcrGetModelInfo?.() || null;
      const isStandaloneMain = isStandaloneMainPromptChain(node);
      const priorPromptState = node?.properties?.pcrPromptState || null;

      const node_ctx = {
        node_prompt: nodePrompt,
        bios,
        tag_format: node?.properties?.pcrTagFormat || "spaces",
        model_hash: modelInfo?.hash || "",
        prompt_style: node?.properties?.pcrPromptStyle || "tags",
        is_standalone_main: isStandaloneMain,
        prompt_state: priorPromptState,
        extra_verbs: extraVerbs,
      };

      // Append the user message to canonical history before sending.
      const historyForRequest = [
        ...historyRaw,
        { role: "user", content: [{ type: "text", text: userText }] },
      ];

      const r = await fetch("/promptchain/ai/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: reqId,
          mode,
          node_ctx,
          history: historyForRequest,
          history_summary: chatSummary,
          images: turnImages.map((i) => ({ hash: i.hash })),
        }),
      });
      const data = await r.json().catch(() => ({}));
      // LLM is done streaming — the remainder of this turn is local
      // proposal application, which doesn't consume backend VRAM.
      // Releasing the flag here lets a concurrent render proceed
      // without us cancelling our own apply step.
      llmCallInFlight = false;
      if (!r.ok) {
        // User-initiated cancel: drop the turn silently. The server
        // returns a non-2xx after a task.cancel() — that's expected,
        // not an error from the user's POV.
        if (!stoppedByUser) {
          errorBanner = data?.error || `HTTP ${r.status}`;
        }
        chat = chat.filter(t => t._turnId !== turnId);
        if (turnImages.length) attachedImages = turnImages;  // let the user retry
        return;
      }

      const newTurns = Array.isArray(data.new_turns) ? data.new_turns : [];
      const newProposals = data.new_proposals || {};
      chat = [...chat, ...newTurns];
      proposals = { ...proposals, ...newProposals };
      historyRaw = Array.isArray(data.history_for_persistence)
        ? data.history_for_persistence
        : historyForRequest;
      if (typeof data.history_summary === "string") {
        chatSummary = data.history_summary;
      }

      // Mode-aware auto-apply. Server already marks status="applied" in
      // auto / auto-run modes; here we mutate the editor doc and (for
      // auto-run) queue the prompt.
      for (const [pid, p] of Object.entries(newProposals)) {
        if (p.status !== "applied") continue;
        applyProposalLocal(pid, p, /*persistApplied=*/false);
      }
      if (mode === "auto-run") {
        const anyApplied = Object.values(newProposals).some(p => p.status === "applied");
        if (anyApplied) {
          try {
            await app.queuePrompt(0, 1);
          } catch (e) {
            const msg = e?.message || String(e);
            showToast("warn", "PromptChain AI", `Queue failed: ${msg}`);
          }
        }
      }
    } catch (e) {
      if (stoppedByUser) {
        // User-initiated cancel — roll back the user's turn (no
        // assistant response to attach to it) without an error banner.
        chat = chat.filter(t => t._turnId !== turnId);
      } else {
        errorBanner = e?.message || "Request failed";
        chat = chat.filter(t => t._turnId !== turnId);
      }
      if (turnImages.length) attachedImages = turnImages;  // let the user retry
    } finally {
      busy = false;
      activeRequestId = "";
      activeMessageText = "";
      llmCallInFlight = false;
      stoppedByUser = false;
      teardownStream();
      // Drain any queued message now that we're no longer busy with
      // chat. The queue may still need to wait on ComfyUI rendering —
      // drainQueueIfReady handles that gate.
      drainQueueIfReady();
    }
  }

  // Fires the queued message when both gates are clear: chat is idle
  // AND ComfyUI is not rendering (VRAM contention guard, applied in
  // every mode — chat and image model can't both have the GPU).
  // Called from handleSubmit's finally and from the comfy "status"
  // listener when queue_remaining transitions to 0.
  async function drainQueueIfReady() {
    if (!queuedMessage && queuedImages.length === 0) return;
    if (busy) return;
    if (await isQueueBusy()) return;
    const pending = queuedMessage;
    queuedMessage = "";
    if (queuedImages.length) {
      attachedImages = queuedImages;
      queuedImages = [];
    }
    inputText = pending;
    queueMicrotask(autoGrowInput);
    handleSubmit();
  }

  function handleKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmitOrQueue();
    }
  }

  // ── Image attach (button / paste / drag-drop) ──────────────────
  let fileInputEl;
  let dragOver = $state(false);

  // Read a File as a data URL, upload it to the user folder, and stage the
  // returned {hash,url}. Pixels go to disk immediately; chat state only
  // ever holds the hash + serve URL.
  async function uploadFile(file) {
    if (!file || !file.type?.startsWith("image/")) return;
    uploadingImage = true;
    try {
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = rej;
        fr.readAsDataURL(file);
      });
      const data = await postJson("/promptchain/ai/upload-image", {
        data: dataUrl, media_type: file.type,
      });
      if (!data?.hash) {
        showToast("warn", "PromptChain AI", data?.error || "Image upload failed");
        return;
      }
      attachedImages = [
        ...attachedImages,
        { hash: data.hash, url: data.url, name: file.name || "image" },
      ];
      focusInput();  // ready to type a request after dropping/pasting
    } catch (e) {
      showToast("warn", "PromptChain AI", `Image upload failed: ${e?.message || e}`);
    } finally {
      uploadingImage = false;
    }
  }

  function removeAttachedImage(hash) {
    attachedImages = attachedImages.filter((i) => i.hash !== hash);
  }

  function pickFile() { fileInputEl?.click(); }
  function onFileChange(e) {
    for (const f of e.target.files || []) uploadFile(f);
    e.target.value = "";  // let the same file be picked again later
  }
  function onComposerPaste(e) {
    for (const it of e.clipboardData?.items || []) {
      if (it.type?.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) { e.preventDefault(); uploadFile(f); }
      }
    }
  }
  function onComposerDrop(e) {
    e.preventDefault();
    dragOver = false;
    for (const f of e.dataTransfer?.files || []) uploadFile(f);
  }
  function onComposerDragOver(e) {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      dragOver = true;
    }
  }
  // dragleave fires when crossing into child elements too; only clear the
  // overlay when the pointer actually leaves the panel.
  function onPanelDragLeave(e) {
    if (!panelEl) return;
    if (!e.relatedTarget || !panelEl.contains(e.relatedTarget)) dragOver = false;
  }

  // Name/provider heuristic for whether the configured model can see
  // images — drives the attach button's enabled state. Permissive by
  // design (the server tries regardless); this just sets expectations.
  function inferVision(cfg) {
    if (cfg?.provider === "cloud") {
      const svc = cfg.cloud?.service || "claude";
      if (svc === "claude") return true;
      return /gpt-4o|gpt-4\.1|o\d|vision|gemini/.test((cfg.cloud?.model || "").toLowerCase());
    }
    return /vl|vision|llava|moondream|minicpm-?v|glm-?4\.?\d*v|gemma3|qwen2?\.?5?-?vl|pixtral|internvl/
      .test((cfg?.local?.model || "").toLowerCase());
  }

  // ── Proposal lifecycle ─────────────────────────────────────────

  function applyProposalLocal(proposalId, proposal, persistApplied = true) {
    const view = node?._pcrEditor;
    const outputText = proposal?.tool_result?.output_text || "";
    const promptState = proposal?.tool_result?.prompt_state;
    if (view && outputText) {
      applyPromptText(view, outputText, node, promptState);
    }
    if (persistApplied) {
      const next = { ...proposals };
      next[proposalId] = {
        ...proposal,
        status: "applied",
        appliedAt: Date.now(),
      };
      proposals = next;
    }
  }

  async function applyProposal(proposalId) {
    const p = proposals[proposalId];
    if (!p) return;
    applyProposalLocal(proposalId, p, /*persistApplied=*/true);
    // If the user switched to auto-run while this proposal was pending
    // (race-recovery accept), honor the queue intent. Mirrors the
    // post-submit auto-queue path.
    if (mode === "auto-run") {
      try {
        await app.queuePrompt(0, 1);
      } catch (e) {
        showToast("warn", "PromptChain AI", `Queue failed: ${e?.message || e}`);
      }
    }
  }

  function rejectProposal(proposalId) {
    const p = proposals[proposalId];
    if (!p) return;
    const next = { ...proposals };
    next[proposalId] = { ...p, status: "rejected" };
    proposals = next;
  }

  function clearChat() {
    chat = [];
    proposals = {};
    historyRaw = [];
    chatSummary = "";
    errorBanner = "";
  }

  // ── Per-turn actions (regenerate / edit-resend) ────────────────
  // User-text messages appear in BOTH the display `chat` array and the
  // canonical `historyRaw`, in the same order (tool_use/tool_result blocks
  // pad historyRaw but never user-text). So the Nth user turn in chat maps
  // to the Nth user-text message in historyRaw — used to rewind both in
  // lockstep when re-running from an earlier point.
  function userTextRawIndices() {
    const idxs = [];
    historyRaw.forEach((m, i) => {
      if ((m?.role || "") !== "user") return;
      const c = m.content;
      const hasText = typeof c === "string"
        || (Array.isArray(c) && c.some((b) => b?.type === "text"));
      if (hasText) idxs.push(i);
    });
    return idxs;
  }

  // Rewind to just before the user turn at chatIndex, then re-run it with
  // (possibly edited) text. Everything after that turn — assistant replies,
  // proposals, canonical tool exchanges — is discarded (standard chat-edit
  // fork semantics). The doc itself isn't reverted; the user re-applies if
  // they want. handleSubmit re-appends the user turn.
  function resendFromUserTurn(chatIndex, newText) {
    if (busy) return;
    const text = (newText ?? "").trim();
    if (!text) return;

    let ordinal = -1;
    for (let i = 0; i <= chatIndex && i < chat.length; i++) {
      if (chat[i]?.role === "user") ordinal++;
    }
    if (ordinal < 0) return;

    const rawIdxs = userTextRawIndices();
    if (ordinal < rawIdxs.length) {
      historyRaw = historyRaw.slice(0, rawIdxs[ordinal]);
    }
    chat = chat.slice(0, chatIndex);
    inputText = text;
    queueMicrotask(autoGrowInput);
    handleSubmit();
  }

  // Re-run the most recent user message unchanged (reroll for a different
  // proposal/reply).
  function regenerateLast() {
    if (busy) return;
    for (let i = chat.length - 1; i >= 0; i--) {
      if (chat[i]?.role === "user") {
        resendFromUserTurn(i, chat[i].text);
        return;
      }
    }
  }

  // ── Visibility / lifecycle ─────────────────────────────────────

  let toggleListener = null;
  function emitToggle(visible) {
    onToggle(visible);
    toggleListener?.(visible);
  }
  // Focus the composer after the DOM settles (panel just shown, or an
  // image just staged) so the user can type immediately.
  function focusInput() {
    tick().then(() => inputEl?.focus());
  }

  // Default the panel open on a fresh main node when a provider is configured —
  // surfaces the flagship feature. Only when there's no explicit per-node
  // preference yet (a node the user closed, or a saved workflow, keeps its
  // state) and the global setting allows it.
  async function maybeAutoOpen() {
    if (!aiAssistantEnabled()) return;  // experimental gate off
    const pref = node.properties?.pcrAiAssistant;
    const setting = app.ui?.settings?.getSettingValue?.("PromptChain.AutoOpenAIAssistant");
    if (pref !== undefined) return;   // explicit per-node state wins
    if (setting === false) return;    // globally disabled
    try {
      // First-run: default to Local if Ollama already serves the recommended
      // model (no-op once any provider is chosen) so a fresh instance resolves
      // before we check, instead of racing the onboarding config write.
      await fetch("/promptchain/ai/auto-configure", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      }).catch(() => {});
      const cfg = await (await fetch("/promptchain/ai/config")).json();
      if (cfg.provider) show();
    } catch {}
  }

  export function show() {
    if (!aiAssistantEnabled()) return;
    isVisible = true;
    if (node.properties) node.properties.pcrAiAssistant = true;
    emitToggle(true);
    probeLocalProvider();
    focusInput();
  }

  // Preflight ping when the panel opens. If the configured local
  // provider isn't answering, try to wake it (server-side spawns
  // `ollama serve`) before showing an error — Windows installs almost
  // always have ollama in PATH, so the typical "user killed Ollama"
  // case recovers in <2s without bothering them.
  async function probeLocalProvider() {
    probing = true;
    try {
      const cfgR = await fetch("/promptchain/ai/config");
      const cfg = await cfgR.json().catch(() => ({}));
      visionCapable = inferVision(cfg);
      // No provider yet — first try to default to Local if Ollama is already
      // serving the recommended model (zero-setup happy path). Only then, if
      // still unconfigured and not dismissed, prompt the user to set one up.
      if (!cfg.provider) {
        const auto = await postJson("/promptchain/ai/auto-configure", {});
        if (auto.configured) {
          visionCapable = true;  // recommended model (qwen3-vl) is vision-capable
          return;
        }
        try {
          const d = await (await fetch("/promptchain/ai-setup/dismissed")).json();
          if (!d.dismissed) window.dispatchEvent(new CustomEvent("promptchain:show-ai-setup"));
        } catch {}
        return;
      }
      if (cfg.provider !== "local") return;
      infoBanner = "Checking AI provider...";
      const test = await postJson("/promptchain/ai/test", { provider: "local" });
      if (test.ok) {
        infoBanner = "";
        return;
      }

      const base = cfg.local?.base_url || "localhost:11434";
      infoBanner = `Ollama down at ${base}, attempting start...`;
      const wake = await postJson("/promptchain/ai/wake-local", {});
      infoBanner = "";
      if (wake.ok) {
        showToast("success", "PromptChain AI", "Started Ollama.");
      } else {
        errorBanner = wake.error
          ? `Couldn't start Ollama at ${base}. ${wake.error}`
          : `Ollama offline at ${base}. Start it and try again.`;
      }
    } catch {
      // best-effort — silent on probe failure
    } finally {
      probing = false;
    }
  }

  async function postJson(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json().catch(() => ({}));
  }
  export function hide() {
    isVisible = false;
    if (node.properties) node.properties.pcrAiAssistant = false;
    emitToggle(false);
  }
  export function toggle() { isVisible ? hide() : show(); }
  export function getIsVisible() { return isVisible; }
  export function setToggleListener(cb) { toggleListener = cb; }

  // Upper bound mirrors ImagePanel.getMaxPanelWidth: row width minus the
  // image panel/dividers. Editor stack is allowed to fully collapse — the
  // panel can cover it. Returns +Infinity until the row has laid out so
  // the initial state isn't accidentally clamped to MIN_WIDTH.
  function getMaxPanelWidth() {
    const row = panelEl?.parentElement;
    const rowWidth = row?.offsetWidth || 0;
    if (!rowWidth) return Number.POSITIVE_INFINITY;
    const imagePanelW = row.querySelector(".pcr-image-panel")?.offsetWidth || 0;
    const imageDividerW = row.querySelector(".pcr-image-divider")?.offsetWidth || 0;
    const dividerW = dividerEl?.offsetWidth || 0;
    return Math.max(MIN_WIDTH, rowWidth - imagePanelW - imageDividerW - dividerW);
  }

  function clampPanelWidth(value) {
    return Math.min(Math.max(MIN_WIDTH, value), getMaxPanelWidth());
  }

  // Divider drag — capture phase, scaled by the litegraph zoom.
  let dividerAc;
  onMount(() => {
    onRegister?.({ show, hide, toggle, getIsVisible, setToggleListener, cleanup });
    if (isVisible) probeLocalProvider();
    else maybeAutoOpen();

    // If the user configures a provider later (e.g. finishes onboarding after
    // this node mounted), re-attempt the auto-open — fixes the first-run race
    // where the node mounts before any provider exists.
    aiConfiguredHandler = () => { if (!isVisible) maybeAutoOpen(); };
    window.addEventListener("promptchain:ai-configured", aiConfiguredHandler);

    // Experimental gate flipped off in Settings while the panel is open —
    // close it; the menubar button it would reopen from is gone too.
    aiEnabledChangedHandler = (e) => {
      if (e.detail?.value !== true && isVisible) hide();
    };
    window.addEventListener("promptchain:ai-assistant-enabled-changed", aiEnabledChangedHandler);

    dividerAc = new AbortController();
    let isDragging = false;
    let startX = 0;
    let startWidth = 0;

    document.addEventListener("pointerdown", (e) => {
      if (!dividerEl || (e.target !== dividerEl && !dividerEl.contains(e.target))) return;
      isDragging = true;
      startX = e.clientX;
      startWidth = panelEl?.offsetWidth || panelWidth;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      const canvas = document.querySelector("canvas.lgraphcanvas");
      if (canvas && typeof e.pointerId === "number") {
        try { if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId); } catch {}
      }
    }, { capture: true, signal: dividerAc.signal });

    document.addEventListener("pointermove", (e) => {
      if (!isDragging) return;
      e.preventDefault();
      e.stopPropagation();
      const inFs = !!document.querySelector(".pcr-fs-overlay");
      const scale = inFs ? 1 : getCanvasScale();
      const delta = (e.clientX - startX) / scale;
      panelWidth = clampPanelWidth(startWidth + delta);
    }, { capture: true, signal: dividerAc.signal });

    document.addEventListener("pointerup", () => {
      if (!isDragging) return;
      isDragging = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (node.properties) node.properties.pcrAiPanelWidth = panelWidth;
    }, { capture: true, signal: dividerAc.signal });

    // Clamp a saved oversized width to current row layout. Updates live
    // state only, not node.properties — the saved preference is preserved
    // so the panel can grow back if the node is later widened.
    requestAnimationFrame(() => { panelWidth = clampPanelWidth(panelWidth); });

    // Drain a queued message as soon as ComfyUI finishes rendering.
    // The "status" event fires after every prompt finishes with the
    // new exec_info.queue_remaining; we only act when there's
    // actually something queued, so the listener is otherwise inert.
    comfyStatusHandler = (e) => {
      const remaining = e?.detail?.exec_info?.queue_remaining;
      if (typeof remaining !== "number") return;
      if (remaining !== 0) return;
      drainQueueIfReady();
    };
    api.addEventListener("status", comfyStatusHandler);

    // VRAM yield: when a render starts mid-chat, cancel chat and
    // re-queue. The handler is mode-agnostic — chat and image gen
    // can't share the GPU regardless of mode.
    comfyExecStartHandler = () => handleComfyExecutionStart();
    api.addEventListener("execution_start", comfyExecStartHandler);
  });

  let comfyStatusHandler = null;
  let comfyExecStartHandler = null;
  let aiConfiguredHandler = null;
  let aiEnabledChangedHandler = null;

  function cleanup() {
    dividerAc?.abort();
  }

  onDestroy(() => {
    dividerAc?.abort();
    if (aiConfiguredHandler) window.removeEventListener("promptchain:ai-configured", aiConfiguredHandler);
    if (aiEnabledChangedHandler) window.removeEventListener("promptchain:ai-assistant-enabled-changed", aiEnabledChangedHandler);
    if (elapsedTimer) clearInterval(elapsedTimer);
    wsUnsub?.();
    if (comfyStatusHandler) {
      api.removeEventListener("status", comfyStatusHandler);
      comfyStatusHandler = null;
    }
    if (comfyExecStartHandler) {
      api.removeEventListener("execution_start", comfyExecStartHandler);
      comfyExecStartHandler = null;
    }
  });

  function resetWidth(e) {
    e.preventDefault();
    e.stopPropagation();
    panelWidth = DEFAULT_WIDTH;
    if (node.properties) node.properties.pcrAiPanelWidth = panelWidth;
  }

  const EMPTY_HINT = "Describe a change to your prompt — \"add red socks\", \"swap to anime style\", \"increase weight on red_socks\". The assistant will propose an edit you can accept, edit automatically, or auto-run.";
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={panelEl}
  class="pcr-ai-panel"
  style:width="{panelWidth}px"
  style:display={isVisible ? "flex" : "none"}
  onpointerdown={(e) => e.stopPropagation()}
  onmousedown={(e) => e.stopPropagation()}
  onclick={(e) => e.stopPropagation()}
  ondblclick={(e) => e.stopPropagation()}
  ondrop={onComposerDrop}
  ondragover={onComposerDragOver}
  ondragleave={onPanelDragLeave}
>
  {#if dragOver}
    <div class="pcr-ai-drop-overlay">
      <div class="pcr-ai-drop-overlay-msg">Drop image to attach</div>
    </div>
  {/if}
  <div class="pcr-ai-panel-header">
    <span class="pcr-ai-panel-icon">{"✨"}</span>
    <span class="pcr-ai-panel-title">AI Assistant</span>
    <span class="pcr-ai-panel-spacer"></span>
    {#if chat.length > 0}
      <span class="pcr-ai-panel-close pcr-ai-panel-clearchat" onclick={clearChat} title="Clear chat">{"⟲"}</span>
    {/if}
    <span class="pcr-ai-panel-close" onclick={hide} title="Close panel">{"✕"}</span>
  </div>

  <div class="pcr-ai-panel-body" bind:this={bodyEl} onscroll={recordPinState}>
    {#if infoBanner}
      <div class="pcr-ai-panel-info">{infoBanner}</div>
    {/if}
    {#if errorBanner}
      <div class="pcr-ai-panel-error">
        <strong>Error</strong>
        <div>{errorBanner}</div>
        <button class="pcr-ai-panel-link" type="button" onclick={() => errorBanner = ""}>Dismiss</button>
      </div>
    {/if}
    <ChatTimeline
      turns={chat}
      {proposals}
      {busy}
      {thinkingState}
      condensed={!!chatSummary}
      emptyHint={EMPTY_HINT}
      suppressEmptyHint={probing}
      onAccept={applyProposal}
      onReject={rejectProposal}
      onRegenerate={regenerateLast}
      onEditResend={resendFromUserTurn}
    />
  </div>

  <div class="pcr-ai-panel-composer">
    {#if queuedMessage}
      <div class="pcr-ai-queued" title="Will send when current generation finishes">
        <span class="pcr-ai-queued-label">Queued</span>
        <span class="pcr-ai-queued-text">{queuedMessage}</span>
        <button
          type="button"
          class="pcr-ai-queued-cancel"
          title="Cancel queued message"
          onclick={() => { queuedMessage = ""; }}
        >×</button>
      </div>
    {/if}
    <div class="pcr-ai-panel-input-card">
      {#if attachedImages.length}
        <div class="pcr-ai-attach-row">
          {#each attachedImages as img (img.hash)}
            <div class="pcr-ai-attach-chip" title={img.name}>
              <img src={img.url} alt={img.name} />
              <button
                type="button"
                class="pcr-ai-attach-remove"
                title="Remove image"
                onclick={() => removeAttachedImage(img.hash)}
              >×</button>
            </div>
          {/each}
          {#if uploadingImage}
            <div class="pcr-ai-attach-chip pcr-ai-attach-loading">…</div>
          {/if}
        </div>
      {/if}
      <textarea
        bind:this={inputEl}
        bind:value={inputText}
        class="pcr-ai-panel-input"
        placeholder="Ask a follow-up, refine, or attach an image..."
        rows="1"
        oninput={autoGrowInput}
        onkeydown={handleKeydown}
        onpaste={onComposerPaste}
      ></textarea>
      <div class="pcr-ai-panel-composer-toolbar">
        <div class="pcr-ai-panel-composer-group">
          <input
            bind:this={fileInputEl}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            style="display:none"
            onchange={onFileChange}
          />
          <button
            type="button"
            class="pcr-ai-attach-btn"
            title={visionCapable ? "Attach image" : "Configured model may not support images"}
            disabled={uploadingImage}
            onclick={pickFile}
          >
            {@html '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>'}
          </button>
          {#if !visionCapable}
            <span class="pcr-ai-vision-warn" title="The configured model may not read images. Switch to a vision model (e.g. qwen3-vl) in AI settings.">⚠</span>
          {/if}
          <CommandsMenu
            {extraVerbs}
            onChangeExtraVerbs={(v) => { extraVerbs = v; }}
          />
          <ModeDropdown
            value={mode}
            onChange={(v) => { mode = v; }}
          />
        </div>
        <div class="pcr-ai-panel-composer-group">
          {#if busy}
            <button
              class="pcr-ai-panel-submit is-stop"
              title="Stop generation"
              type="button"
              onclick={handleStop}
            >
              {@html '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'}
            </button>
          {:else}
            <button
              class="pcr-ai-panel-submit"
              title="Send"
              type="button"
              disabled={!canSubmit}
              onclick={handleSubmitOrQueue}
            >
              {@html '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>'}
            </button>
          {/if}
        </div>
      </div>
    </div>
  </div>
</div>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={dividerEl}
  class="pcr-ai-divider"
  style:display={isVisible ? "flex" : "none"}
  ondblclick={resetWidth}
  title="Drag to resize · double-click to reset"
></div>
