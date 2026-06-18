// Sidebar bridge — lazy-loads the Svelte asset browser and registers it as a ComfyUI sidebar tab.

import { api } from "../../../scripts/api.js";
import { app } from "../../../scripts/app.js";
import { getWorkflowId } from "./workflow-id.js";
import { ensureSvelteCSS, createModuleLoader } from "./lazy-load.js";

const ensureCSS = ensureSvelteCSS;
const loadModule = createModuleLoader(() => import("./svelte/promptchain-sidebar.js"));

// inject sidebar icon CSS (chain-link SVG matching legacy style)
function ensureIconCSS() {
  if (document.getElementById("pcr-sidebar-icon-css")) return;
  const style = document.createElement("style");
  style.id = "pcr-sidebar-icon-css";
  style.textContent = `
    .pcr-sidebar-icon {
      display: inline-block;
      width: 24px;
      height: 24px;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' fill='none' viewBox='0 0 24 24'%3E%3Cpath stroke='%23a1a1aa' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M13.213 9.787a3.391 3.391 0 0 0-4.795 0l-3.425 3.426a3.39 3.39 0 0 0 4.795 4.794l.321-.304m-.321-4.49a3.39 3.39 0 0 0 4.795 0l3.424-3.426a3.39 3.39 0 0 0-4.794-4.795l-1.028.961'/%3E%3C/svg%3E");
      background-size: contain;
      background-repeat: no-repeat;
      background-position: center;
    }
    .side-bar-button-selected .pcr-sidebar-icon {
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' fill='none' viewBox='0 0 24 24'%3E%3Cpath stroke='%23ffffff' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M13.213 9.787a3.391 3.391 0 0 0-4.795 0l-3.425 3.426a3.39 3.39 0 0 0 4.795 4.794l.321-.304m-.321-4.49a3.39 3.39 0 0 0 4.795 0l3.424-3.426a3.39 3.39 0 0 0-4.794-4.795l-1.028.961'/%3E%3C/svg%3E");
    }
    .promptchain-browser-tab-button .side-bar-button-title,
    .promptchain-browser-tab-button .side-bar-button-label {
      line-height: 1.1;
      white-space: pre-line;
    }
    .promptchain-browser-tab-button .side-bar-button-content {
      gap: 2px;
    }
    .promptchain-browser-tab-button .sidebar-icon-wrapper {
      display: flex;
      align-items: flex-end;
      justify-content: center;
      height: 100%;
    }
  `;
  document.head.appendChild(style);
}

let gutterObserver = null;
let visObserver = null;
let renderAc = null;

function watchGutter(container) {
  // find the adjacent gutter once the container is in the DOM
  const apply = () => {
    const panel = container.closest(".side-bar-panel");
    if (!panel) return;
    const gutter =
      (panel.nextElementSibling?.classList.contains("p-splitter-gutter") && panel.nextElementSibling) ||
      (panel.previousElementSibling?.classList.contains("p-splitter-gutter") && panel.previousElementSibling) ||
      null;
    if (!gutter) return;

    const narrow = () => gutter.style.setProperty("width", "2px", "important");
    const restore = () => gutter.style.removeProperty("width");

    // apply immediately if visible
    if (container.offsetParent !== null) narrow();

    gutterObserver = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) narrow(); else restore();
    }, { threshold: 0.01 });
    gutterObserver.observe(container);
  };

  requestAnimationFrame(apply);
}

function unwatchGutter() {
  gutterObserver?.disconnect();
  gutterObserver = null;
}

export function createSidebarTab() {
  ensureIconCSS();

  const TAB_ID = "promptchain-browser";
  const tabDef = {
      id: "promptchain-browser",
      type: "custom",
      title: "Prompt\nChain",
      icon: "pcr-sidebar-icon",
      tooltip: "PromptChain Browser",

    render(container) {
      ensureCSS();
      watchGutter(container);
      container.style.height = "100%";
      container.style.overflow = "hidden";
      try { localStorage.setItem("pcr-sidebar-open", "1"); } catch {}
      loadModule().then((mod) => {
        // abort on destroy() to tear down listeners bound via signal below —
        // ComfyUI's ExtensionSlot remounts on every sidebar toggle, so
        // without this the executed-handler stack grows per toggle.
        renderAc = new AbortController();
        const signal = renderAc.signal;

        // event-driven refresh: sidebar registers callbacks, bridge fires them on events
        const refreshListeners = new Set();
        const fireRefresh = (scope, detail) => { for (const cb of refreshListeners) cb(scope, detail); };

        // collect output filenames during execution, fire targeted refresh on success
        const pendingOutputs = new Map();
        api.addEventListener("executed", ({ detail }) => {
          if (!detail?.output) return;
          if (!pendingOutputs.has(detail.prompt_id)) pendingOutputs.set(detail.prompt_id, []);
          const list = pendingOutputs.get(detail.prompt_id);
          for (const key of ["images", "video", "audio"]) {
            const arr = detail.output[key];
            if (!Array.isArray(arr)) continue;
            for (const item of arr) {
              if (item.filename && item.type === "output") {
                list.push({ filename: item.filename, subfolder: item.subfolder || "" });
              }
            }
          }
        }, { signal });
        api.addEventListener("execution_success", ({ detail }) => {
          const files = pendingOutputs.get(detail.prompt_id) || [];
          pendingOutputs.delete(detail.prompt_id);
          fireRefresh("output", { files });
          fireRefresh("workflows", { thumbnailUpdate: true, workflowId: getWorkflowId() });
        }, { signal });
        api.addEventListener("execution_error", ({ detail }) => {
          pendingOutputs.delete(detail.prompt_id);
        }, { signal });
        api.addEventListener("execution_interrupted", ({ detail }) => {
          pendingOutputs.delete(detail.prompt_id);
        }, { signal });

        // all scopes: refresh when sidebar tab becomes visible (covers external file changes)
        let wasHidden = true;
        visObserver = new IntersectionObserver((entries) => {
          const visible = entries[0]?.isIntersecting;
          if (visible && wasHidden) fireRefresh(null, { visibility: true });
          wasHidden = !visible;
        }, { threshold: 0.1 });
        visObserver.observe(container);

        // input: refresh after drag-to-canvas uploads (fired by asset-drop.js)
        window.addEventListener("promptchain:input-changed", (e) => fireRefresh("input", e.detail || null), { signal });
        window.addEventListener("promptchain:file-deleted", (e) => {
          fireRefresh(e.detail?.scope || "output", { removedPaths: e.detail?.paths || [] });
        }, { signal });

        // workflows: refresh after workflow save/save-as (fired by save interceptor)
        window.addEventListener("promptchain:workflows-changed", (e) => fireRefresh("workflows", e.detail || null), { signal });

        mod.mountSidebar(container, {
          apiURL: (path) => api.apiURL(path),
          fetchApi: (path, opts) => api.fetchApi(path, opts),
          toast: (severity, detail) => {
            app.extensionManager?.toast?.add({ severity, summary: "PromptChain", detail, life: 4000 });
          },
          logoUrl: new URL("../logo.png", import.meta.url).href,
          onSubscribeRefresh: (cb) => { refreshListeners.add(cb); return () => refreshListeners.delete(cb); },
          onSubscribeDedup: (cb) => {
            const handler = ({ detail }) => cb(detail);
            api.addEventListener("promptchain.dedup.progress", handler);
            return () => api.removeEventListener("promptchain.dedup.progress", handler);
          },
          onAddNode: async () => {
            if (!app.graph) {
              await app.loadGraphData({ last_node_id: 0, last_link_id: 0, nodes: [], links: [], groups: [], config: {}, extra: {}, version: 0.4 });
            }
            const LG = window.LiteGraph || globalThis.LiteGraph;
            if (!LG || !app.graph) return;
            const node = LG.createNode("PromptChain_PromptChain");
            if (!node) return;
            const canvas = app.canvas;
            if (canvas?.ds && canvas.canvas) {
              const rect = canvas.canvas.getBoundingClientRect();
              const gx = (rect.width / 2) / canvas.ds.scale - canvas.ds.offset[0];
              const gy = (rect.height / 2) / canvas.ds.scale - canvas.ds.offset[1];
              node.pos = [gx - 150, gy - 50];
            } else {
              node.pos = [100, 100];
            }
            app.graph.add(node);
            app.graph.setDirtyCanvas(true, true);
            canvas?.selectNode(node);
          },
          onCreateWorkflow: async (workflowName, template, modelFilename, subfolder) => {
            const workflowId = crypto.randomUUID();
            const displayName = subfolder ? `${subfolder}/${workflowName}` : workflowName;

            if (template) {
              // template mode: seed with a PromptChain node, then apply template
              const { applyTemplate } = await import("./model-bridge.js");
              const seed = {
                id: workflowId, last_node_id: 1, last_link_id: 0,
                nodes: [{
                  id: 1, type: "PromptChain_PromptChain",
                  pos: [50, 50],
                  flags: {}, order: 0, mode: 0,
                  inputs: [], outputs: [],
                  properties: {}, widgets_values: [],
                }],
                links: [], groups: [], config: {},
                extra: { promptchain: { id: workflowId, name: workflowName } },
                version: 0.4,
              };
              await app.loadGraphData(seed, true, true, displayName);

              const pcNode = app.graph._nodes.find(n =>
                n.type === "PromptChain_PromptChain" || n.comfyClass === "PromptChain_PromptChain"
              );
              if (pcNode) {
                applyTemplate(pcNode, template, modelFilename || "");
                app.graph.setDirtyCanvas(true, true);
              }
            } else {
              // blank mode: empty canvas
              const seed = {
                id: workflowId, last_node_id: 0, last_link_id: 0,
                nodes: [], links: [], groups: [], config: {},
                extra: {}, version: 0.4,
              };
              await app.loadGraphData(seed, true, true, displayName);
            }

            // serialize and save the fully-connected graph
            const dir = subfolder ? `workflows/${subfolder}` : "workflows";
            const savePath = `${dir}/${workflowName}.json`;
            try {
              const graphData = app.graph.serialize();
              graphData.extra = graphData.extra || {};
              graphData.extra.promptchain = { id: workflowId, name: workflowName };
              const resp = await api.fetchApi(`/userdata/${encodeURIComponent(savePath)}?full_info=true`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(graphData),
              });

              // loadGraphData created a temporary workflow (size=-1). Patch it
              // to persisted so subsequent Ctrl+S sends overwrite:true and
              // doesn't 409 against the file we just created.
              const store = app.extensionManager?.workflow;
              const active = store?.activeWorkflow;
              if (active && active.isTemporary && resp.ok) {
                const info = await resp.json().catch(() => null);
                if (info && typeof info === "object") {
                  active.size = info.size;
                  active.lastModified = info.modified;
                } else {
                  active.size = 1;
                  active.lastModified = Date.now();
                }
                // sync content so the workflow isn't marked as modified
                const saved = JSON.stringify(graphData);
                active.content = saved;
                active.originalContent = saved;
              }
              store?.syncWorkflows?.();
            } catch (e) {
              console.error("[PromptChain] Failed to save new workflow:", e);
            }

            window.dispatchEvent(new CustomEvent("promptchain:workflows-changed", {
              detail: { path: subfolder ? `${subfolder}/${workflowName}` : workflowName },
            }));
          },
          onOpenSettings: () => {
            app.extensionManager?.command?.execute?.("Comfy.ShowSettingsDialog");
          },
          onOpenViewer: async (item, scope, browsePath, siblings, startIndex, opts) => {
            const { openViewer } = await import("./viewer-bridge.js");
            const list = siblings?.length ? siblings : [item];
            const images = list.map(i => {
              if (i.hash) return { hash: i.hash, filename: i.name, subfolder: "" };
              const url = api.apiURL(`/promptchain/browse/preview?scope=${scope}&path=${encodeURIComponent(i.path)}`);
              return { hash: i.path, filename: i.name, subfolder: "", _directUrl: url, _browseScope: scope, _browsePath: i.path };
            });
            openViewer(images, startIndex ?? 0, "", async (hash) => {
              const img = images.find(i => i.hash === hash);
              if (!img) return;
              const path = img._browsePath || (img.subfolder ? `${img.subfolder}/${img.filename}` : img.filename);
              await api.fetchApi("/promptchain/browse/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope, paths: [path] }),
              });
              // targeted removal in sidebar
              fireRefresh(scope, { removedPaths: [path] });
              // notify generated panel
              window.dispatchEvent(new CustomEvent("promptchain:file-deleted", {
                detail: { scope, paths: [path] },
              }));
            }, opts);
          },
          onLoadWorkflow: async (item, scope) => {
            try {
              const store = app.extensionManager?.workflow;
              // Switch to existing tab if this workflow is already open
              // Store paths are prefixed with "workflows/" (ComfyWorkflow.basePath)
              const storePath = "workflows/" + item.path;
              const existing = store?.openWorkflows?.find(w => w.path === storePath);
              if (existing) {
                if (!store.isActive(existing)) await store.openWorkflow(existing);
                return;
              }

              const resp = await api.fetchApi(`/promptchain/browse/preview?scope=${scope}&path=${encodeURIComponent(item.path)}`);
              if (!resp.ok) { console.error("[PromptChain] Workflow fetch failed:", resp.status); return; }
              const data = await resp.json();
              // afterLoadNewGraph prepends "workflows/" already — don't double it
              await app.loadGraphData(data, true, true, item.path);
              // sync ComfyUI's workflow store so it tracks the opened file
              store?.syncWorkflows?.();
            } catch (e) {
              console.error("[PromptChain] Failed to load workflow:", e);
            }
          },
        });
      });
    },

      destroy() {
        unwatchGutter();
        visObserver?.disconnect();
        visObserver = null;
        renderAc?.abort();
        renderAc = null;
        try { localStorage.setItem("pcr-sidebar-open", "0"); } catch {}
        loadModule().then((mod) => mod.destroySidebar()).catch(e => console.error("[PromptChain] sidebar destroy failed:", e));
      },
  };

  // register once extensionManager exists
  const register = () => {
    if (!app.extensionManager) {
      requestAnimationFrame(register);
      return;
    }
    app.extensionManager.registerSidebarTab(tabDef);

    // restore sidebar open state from last session
    try {
      if (localStorage.getItem("pcr-sidebar-open") === "1") {
        const selector = `.${TAB_ID}-tab-button`;
        const existing = document.querySelector(selector);
        if (existing && !existing.classList.contains("side-bar-button-selected")) {
          existing.click();
        } else if (!existing) {
          const observer = new MutationObserver(() => {
            const btn = document.querySelector(selector);
            if (!btn) return;
            observer.disconnect();
            if (!btn.classList.contains("side-bar-button-selected")) btn.click();
          });
          observer.observe(document.body, { childList: true, subtree: true });
        }
      }
    } catch {}
  };
  requestAnimationFrame(register);

  // canvas drop handler — uses the battle-tested legacy asset-drop module
  import("./asset-drop.js").then(m => m.setupAssetDropHandler()).catch(e => console.error("[PromptChain] asset-drop setup failed:", e));
}
