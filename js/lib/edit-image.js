// Viewer "Edit": quick airbrush touch-up over the image before a follow-up
// inpaint or upscale. The modal paints on a layer over the untouched source;
// Save composites them in the browser and POSTs the PNG to /promptchain/
// save-edited-image, which re-attaches the parent's embedded prompt/workflow
// chunks (so the result still anchors inpaint/upscale grafts) and records it
// with parent lineage.

import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { sourceSavePrefixInfo } from "./upscale-from-image.js";
import { refreshEntryWorkflows } from "./history.js";

function toast(severity, detail) {
  app.extensionManager?.toast?.add({ severity, summary: "Edit", detail, life: 7000 });
}

function freshWorkflowId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
}

export async function prepareEdit(entry) {
  const params = entry._browsePath != null
    ? `scope=${encodeURIComponent(entry._browseScope || "output")}&path=${encodeURIComponent(entry._browsePath)}`
    : `hash=${encodeURIComponent(entry.hash)}`;
  let data = null;
  try {
    const res = await api.fetchApi(`/promptchain/image-workflow?${params}`);
    if (res.ok) data = await res.json();
  } catch (e) {
    console.error("[PromptChain] image-workflow fetch failed", e);
  }
  if (!data?.input_ref) {
    toast("error", "Couldn't read the image on the server.");
    return null;
  }
  const { prefix } = sourceSavePrefixInfo(data.workflow);
  // No workflow-derived prefix (image dropped into a sidebar folder, no
  // embedded SaveImage): default to the image's own folder instead of the
  // generic edit/ bucket — but only when that folder is in the OUTPUT tree,
  // since save-edited-image always writes output-side. A workflows/input
  // folder name would mint a phantom output subfolder.
  const sourceFolder = entry._browsePath != null
    ? ((entry._browseScope || "output") === "output"
        ? entry._browsePath.split("/").slice(0, -1).join("/")
        : "")
    : (entry.subfolder || "");
  const defaultSavePrefix = prefix
    ? (prefix.endsWith("_edit") ? prefix : `${prefix}_edit`)
    : sourceFolder
      ? `${sourceFolder}/%date:yyyy-MM-dd_hhmmss%`
      : "edit/edit";
  return { data, caps: { defaultSavePrefix } };
}

// Layer-stack sidecar (the "don't-flatten = keep your PSD" persistence). Keyed
// by the SAVED image's content hash; written AFTER the flat PNG save so it never
// blocks the gallery record. planes = [{ name:"L3.png", blob }]. Best-effort —
// the flat PNG is always the source of truth, a failed doc write is non-fatal.
export async function persistEditDoc(hash, manifest, planes) {
  if (!hash || !manifest) return;
  try {
    const form = new FormData();
    form.append("manifest", JSON.stringify(manifest));
    for (const { name, blob } of planes) form.append(name, new File([blob], name, { type: "image/png" }));
    await api.fetchApi(`/promptchain/edit-doc/${hash}`, { method: "POST", body: form });
  } catch (e) {
    console.warn("[PromptChain] edit-doc persist failed", e);
  }
}

export async function fetchEditDoc(hash) {
  if (!hash) return null;
  try {
    const res = await api.fetchApi(`/promptchain/edit-doc/${hash}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

export function editDocPlaneUrl(hash, file) {
  return `/promptchain/edit-doc/${encodeURIComponent(hash)}/${encodeURIComponent(file)}`;
}

export async function saveEditedImage(prepared, blob, prefix) {
  try {
    const form = new FormData();
    form.append("image", new File([blob], "edit.png", { type: "image/png" }));
    form.append("prefix", prefix || "");
    form.append("parent_filename", prepared?.data?.input_ref || "");
    form.append("workflow_id", freshWorkflowId());
    const res = await api.fetchApi("/promptchain/save-edited-image", { method: "POST", body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => null);
      toast("error", `Save failed: ${err?.error || res.status}`);
      return null;
    }
    const entry = await res.json();
    refreshEntryWorkflows(entry);
    return entry;
  } catch (e) {
    toast("error", `Save failed: ${e.message}`);
    return null;
  }
}
