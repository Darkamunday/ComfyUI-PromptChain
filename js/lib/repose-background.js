// Re-pose background runner. Builds the recipe graph off-screen, queues it, and
// tracks execution — the user's open workflow is NEVER touched (same contract as
// upscale-background.js). On success the result is recorded as a lineage CHILD of
// the source image (parent_filename) so it appears under the family in the gallery.

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { recordGeneration, externallyTrackedPrompts, armExternalQueue, disarmExternalQueue } from "./history.js";
import { buildReposeGraph } from "./repose-from-image.js";

function toast(severity, detail, life = 8000) {
  app.extensionManager?.toast?.add({ severity, summary: "Re-pose", detail, life });
}

function createTracker() {
  const listeners = new Set();
  const tracker = {
    state: { phase: "building" },
    subscribe(fn) { listeners.add(fn); fn(tracker.state); return () => listeners.delete(fn); },
    emit(state) { tracker.state = state; for (const fn of listeners) { try { fn(state); } catch {} } },
  };
  return tracker;
}

function viewUrl(img) {
  if (!img?.filename) return "";
  const q = new URLSearchParams({ filename: img.filename, subfolder: img.subfolder || "", type: img.type || "output" });
  return api.apiURL ? api.apiURL(`/view?${q}`) : `/view?${q}`;
}

// opts: { recipe (caps entry), modelFilename, prompt, neg, sampler{}, controlMapFilename,
//         referenceFilename, parentFilename }
export function runReposeInBackground(opts) {
  const tracker = createTracker();
  const run = { cancelled: false, promptId: null, executionStarted: false, cleanup: null };

  tracker.cancel = async () => {
    if (run.cancelled) return;
    run.cancelled = true;
    tracker.emit({ phase: "cancelled" });
    toast("info", "Re-pose cancelled.");
    try {
      if (run.promptId) {
        if (run.executionStarted) await api.interrupt();
        else {
          await api.fetchApi("/queue", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ delete: [run.promptId] }),
          });
          run.cleanup?.();
        }
      }
    } catch (e) { console.warn("[Re-pose] cancel request failed", e); }
  };

  (async () => {
    const recipe = opts.recipe;
    tracker.emit({ phase: "building" });
    const built = await buildReposeGraph({
      templateId: recipe.templateId,
      modelFilename: opts.modelFilename,
      loraStrength: opts.loraStrength ?? recipe.loraStrength,
      referenceFilename: opts.referenceFilename,
      controlMapFilename: opts.controlMapFilename,
      promptDoc: opts.promptDoc,
      sampler: opts.sampler,
      megapixels: opts.megapixels ?? recipe.megapixels,
    });
    if (!built) { tracker.emit({ phase: "error", message: "couldn't build the re-pose graph" }); return; }
    const { graph, workflowId } = built;
    if (run.cancelled) return;

    tracker.emit({ phase: "queueing" });
    const serialized = await app.graphToPrompt(graph);
    // DIAGNOSTIC: the raw doc handed to PromptChain_PromptChain in the queued
    // prompt. The server's compile_prompt turns this into the encoder strings —
    // strips `//` sections, splits on `Negative Prompt:`, expands tags/outfits —
    // the same single-source compile inpaint/upscale route through. The encoder
    // text now arrives as a LINK from this node (not a literal), so the doc is
    // verified HERE; the compiled result is the server's job.
    try {
      const pcEntry = Object.entries(serialized.output).find(([, n]) => n.class_type === "PromptChain_PromptChain");
      console.log("[Re-pose] prompt → PromptChain compile:", pcEntry?.[1]?.inputs?.prompt ?? "(no PromptChain node!)");
    } catch { /* logging only */ }
    armExternalQueue();
    let promptId = null;
    try {
      const previewMethod = app.extensionManager?.setting?.get?.("Comfy.Execution.PreviewMethod")
        ?? app.ui?.settings?.getSettingValue?.("Comfy.Execution.PreviewMethod") ?? "default";
      const res = await api.queuePrompt(0, { output: serialized.output, workflow: serialized.workflow }, { previewMethod });
      promptId = res?.prompt_id || null;
    } catch (e) {
      disarmExternalQueue();
      const message = e?.response?.error?.message || e?.message || "queue rejected the prompt";
      tracker.emit({ phase: "error", message });
      toast("error", `Re-pose failed to queue: ${message}`);
      return;
    }
    if (promptId) externallyTrackedPrompts.add(promptId);
    disarmExternalQueue();
    run.promptId = promptId;
    if (!promptId) { tracker.emit({ phase: "error", message: "queue returned no prompt id" }); return; }
    if (run.cancelled) {
      try {
        await api.fetchApi("/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ delete: [promptId] }) });
      } catch {}
      externallyTrackedPrompts.delete(promptId);
      return;
    }

    let lastImage = null, lastPreview = null;
    const onStart = ({ detail }) => { if (detail?.prompt_id === promptId) run.executionStarted = true; };
    const onProgress = ({ detail }) => {
      // Scope to OUR prompt: a previous (cancelled) render still draining on the
      // GPU emits step events too — without this filter its steps drive THIS run's
      // bar and it looks like the render "resumed where it left off". Also mark
      // executionStarted here so a fast Cancel reliably interrupts (covers a
      // missed execution_start event).
      if (detail?.prompt_id !== promptId || run.cancelled) return;
      run.executionStarted = true;
      // Preserve previewUrl across ticks so the bar and the streaming preview coexist.
      tracker.emit({ ...tracker.state, phase: "running", value: detail?.value || 0, max: detail?.max || 0 });
    };
    const onPreview = ({ detail }) => {
      // b_preview carries no prompt_id — gate on OUR run actually executing so a
      // still-draining previous run's preview frames don't bleed into this one.
      if (run.cancelled || !run.executionStarted || !(detail instanceof Blob)) return;
      if (lastPreview) URL.revokeObjectURL(lastPreview);
      lastPreview = URL.createObjectURL(detail);
      tracker.emit({ ...tracker.state, phase: "running", previewUrl: lastPreview });
    };
    const onExecuted = ({ detail }) => {
      if (detail?.prompt_id !== promptId || !detail?.output?.images?.length) return;
      lastImage = detail.output.images[detail.output.images.length - 1];
    };
    const cleanup = () => {
      api.removeEventListener("execution_start", onStart);
      api.removeEventListener("progress", onProgress);
      api.removeEventListener("b_preview", onPreview);
      api.removeEventListener("executed", onExecuted);
      api.removeEventListener("execution_success", onSuccess);
      api.removeEventListener("execution_error", onError);
      api.removeEventListener("execution_interrupted", onError);
      if (promptId) externallyTrackedPrompts.delete(promptId);
    };
    run.cleanup = cleanup;
    const onSuccess = async ({ detail }) => {
      if (detail?.prompt_id !== promptId) return;
      cleanup();
      if (!lastImage) { tracker.emit({ phase: "error", message: "re-pose produced no image" }); return; }
      const entry = await recordGeneration(workflowId, lastImage.filename, lastImage.subfolder, "output", {
        parent_filename: opts.parentFilename || "",
      });
      tracker.emit({ phase: "done", resultUrl: viewUrl(lastImage), resultHash: entry?.hash || null, entry });
    };
    const onError = ({ detail }) => {
      if (detail?.prompt_id !== promptId) return;
      cleanup();
      tracker.emit({ phase: "error", message: detail?.exception_message || "re-pose render failed" });
    };
    api.addEventListener("execution_start", onStart);
    api.addEventListener("progress", onProgress);
    api.addEventListener("b_preview", onPreview);
    api.addEventListener("executed", onExecuted);
    api.addEventListener("execution_success", onSuccess);
    api.addEventListener("execution_error", onError);
    api.addEventListener("execution_interrupted", onError);
    tracker.emit({ phase: "running" });
  })().catch((e) => {
    console.error("[Re-pose] background run failed", e);
    tracker.emit({ phase: "error", message: e?.message || "unexpected failure" });
    toast("error", `Re-pose failed: ${e?.message || e}`);
  });

  return tracker;
}
