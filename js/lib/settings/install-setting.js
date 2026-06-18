// Inline render for the PromptChain.Install setting row — a health dashboard +
// per-feature picker over the unified section installer. Mirrors ai-setting.js:
// builds plain DOM, streams installs over SSE, and offers a restart when a node
// pack was added. This and the in-app install modal both read the same
// /promptchain/install/sections registry, so the two entry points never drift.

const BTN = "padding:5px 14px;border:none;border-radius:4px;background:var(--comfy-input-bg,#333);color:var(--input-text,#ddd);font-size:12px;cursor:pointer;white-space:nowrap;";
const BTN_PRIMARY = BTN + "background:#0e639c;color:#fff;";

const HEALTH_DOT = { installed: "#4ec96b", partial: "#e0a13a", missing: "#888" };

// Reads an SSE POST stream, invoking onEvent for each parsed `data:` payload.
// Pre-stream failures return JSON {error} with a non-2xx status — thrown here.
async function streamSSE(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const data = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!data) continue;
      try { onEvent(JSON.parse(data.slice(5).trim())); } catch {}
    }
  }
}

export function renderInstallSetting() {
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex;flex-direction:column;gap:10px;width:100%;";

  const intro = document.createElement("div");
  intro.style.cssText = "font-size:12px;color:#888;";
  intro.textContent = "Install the 3rd-party features PromptChain can use. Pick only what you need — each downloads independently and skips anything already present.";
  wrap.append(intro);

  const updatesEl = document.createElement("div");
  wrap.append(updatesEl);

  const list = document.createElement("div");
  list.style.cssText = "display:flex;flex-direction:column;gap:8px;";
  wrap.append(list);

  const progress = document.createElement("div");
  progress.style.cssText = "display:none;flex-direction:column;gap:2px;padding:8px 10px;background:#111;border:1px solid #333;border-radius:4px;font-family:ui-monospace,monospace;font-size:11px;color:#9bb;max-height:150px;overflow:auto;";
  wrap.append(progress);

  const footer = document.createElement("div");
  footer.style.cssText = "display:flex;align-items:center;gap:10px;";
  const recheck = document.createElement("button");
  recheck.textContent = "Re-check";
  recheck.style.cssText = BTN;
  recheck.addEventListener("click", load);
  const status = document.createElement("span");
  status.style.cssText = "font-size:12px;color:#888;";
  footer.append(recheck, status);
  wrap.append(footer);

  let needsRestart = false;

  function logLine(text) {
    progress.style.display = "flex";
    const line = document.createElement("div");
    line.textContent = text;
    progress.append(line);
    progress.scrollTop = progress.scrollHeight;
  }

  function showRestart() {
    if (footer.querySelector(".pcr-inst-restart")) return;
    const r = document.createElement("button");
    r.className = "pcr-inst-restart";
    r.textContent = "Restart ComfyUI";
    r.style.cssText = BTN_PRIMARY;
    r.addEventListener("click", () => {
      r.disabled = true;
      r.textContent = "Restarting…";
      fetch("/promptchain/system/restart", { method: "POST" }).catch(() => {});
      status.textContent = "Restarting… reload the page when it reconnects.";
    });
    footer.append(r);
  }

  async function installMembers(sectionId, members, btn) {
    btn.disabled = true;
    btn.textContent = "Installing…";
    progress.replaceChildren();
    try {
      await streamSSE("/promptchain/install/install", { section: sectionId, members }, (evt) => {
        if (evt.error) logLine("✘ " + evt.error);
        else if (evt.member) logLine("▸ " + evt.member + (evt.state === "installed" ? " (already installed)" : ""));
        else if (evt.line) logLine(evt.line);
        else if (evt.stage === "download") logLine(`  downloading ${evt.file ?? ""} ${evt.pct ?? 0}%`);
        else if (evt.stage === "prefetch") logLine(`  pre-fetching ${evt.file ?? "extra weights"}`);
        else if (evt.stage) logLine("  " + evt.stage);
        if (evt.done) { if (evt.needs_restart) needsRestart = true; logLine("✓ done"); }
      });
    } catch (e) {
      logLine("✘ " + (e.message || "install failed"));
    } finally {
      btn.disabled = false;
      btn.textContent = "Install";
      await load();
      if (needsRestart) showRestart();
    }
  }

  function renderSection(sec) {
    const card = document.createElement("div");
    card.style.cssText = "display:flex;flex-direction:column;gap:6px;padding:10px 12px;background:rgba(255,255,255,0.03);border-radius:4px;";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:8px;";
    const dot = document.createElement("span");
    dot.textContent = "●";
    dot.style.cssText = `color:${HEALTH_DOT[sec.health] || HEALTH_DOT.missing};flex:none;`;
    const label = document.createElement("span");
    label.innerHTML = `${sec.label} <span style="font-weight:400;opacity:.55;font-size:11px;">${sec.size || ""}</span>`;
    label.style.cssText = "font-size:13px;color:var(--input-text,#ddd);font-weight:500;";
    const stat = document.createElement("span");
    stat.textContent = sec.present
      ? "Installed"
      : (sec.installed_count ? `${sec.installed_count} of ${sec.total} installed` : "Not installed");
    stat.style.cssText = "font-size:11px;color:#888;margin-left:auto;";
    head.append(dot, label, stat);
    card.append(head);

    const desc = document.createElement("div");
    desc.textContent = sec.desc;
    desc.style.cssText = "font-size:11px;color:#888;";
    card.append(desc);

    // Multi-member sections (ControlNet, PuLID) expose a checkbox per family;
    // only the missing ones are tickable. Single-member sections install whole.
    const checks = [];
    if (sec.members.length > 1) {
      const memWrap = document.createElement("div");
      memWrap.style.cssText = "display:flex;flex-direction:column;gap:3px;padding-left:18px;";
      for (const m of sec.members) {
        const row = document.createElement("label");
        row.style.cssText = "display:flex;align-items:center;gap:7px;font-size:12px;color:#bbb;cursor:pointer;";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        if (m.state === "installed" || m.state === "needs_restart") {
          cb.checked = true;
          cb.disabled = true;
        } else {
          checks.push({ cb, key: m.key || m.target });
        }
        const suffix = m.state === "installed" ? " ✓"
          : m.state === "needs_restart" ? " (restart to finish)" : "";
        const t = document.createElement("span");
        t.textContent = m.label + suffix;
        row.append(cb, t);
        memWrap.append(row);
      }
      card.append(memWrap);
    }

    if (sec.deferred && sec.deferred.length) {
      const d = document.createElement("div");
      d.style.cssText = "font-size:11px;color:#c8a26a;padding-left:18px;";
      d.textContent = "Downloads later on first use: " + sec.deferred.map((x) => `${x.label} (${x.size})`).join("; ");
      card.append(d);
    }

    const hasMissing = sec.members.some((m) => m.state === "missing");
    if (hasMissing) {
      const actions = document.createElement("div");
      actions.style.cssText = "display:flex;gap:8px;padding-top:2px;";
      const btn = document.createElement("button");
      btn.textContent = "Install";
      btn.style.cssText = BTN_PRIMARY;
      btn.addEventListener("click", () => {
        let members = null; // null = every missing member of the section
        if (checks.length) {
          members = checks.filter((c) => c.cb.checked).map((c) => c.key);
          if (!members.length) { status.textContent = "Tick at least one option to install."; return; }
        }
        installMembers(sec.id, members, btn);
      });
      actions.append(btn);
      card.append(actions);
    } else if (sec.needs_restart) {
      const note = document.createElement("div");
      note.style.cssText = "font-size:11px;color:#e0a13a;";
      note.textContent = "Files present — restart ComfyUI to finish loading.";
      card.append(note);
      showRestart();
    }

    return card;
  }

  async function load() {
    status.textContent = "Checking…";
    status.style.color = "#888";
    list.replaceChildren();
    updatesEl.replaceChildren();
    try {
      const data = await (await fetch("/promptchain/install/sections")).json();
      for (const sec of data.sections || []) list.append(renderSection(sec));
      status.textContent = "";
    } catch (e) {
      status.textContent = "✗ couldn't load features: " + (e.message || "error");
      status.style.color = "#e55";
      return;
    }
    try {
      const u = await (await fetch("/promptchain/install/updates")).json();
      if (u.updates && u.updates.length) {
        const box = document.createElement("div");
        box.style.cssText = "font-size:12px;color:#c8a26a;padding:8px 10px;background:rgba(200,162,106,0.08);border:1px solid rgba(200,162,106,0.25);border-radius:4px;";
        box.textContent = `Update available for bundled pack(s): ${u.updates.map((x) => x.pack).join(", ")}. Reinstall the section, then restart, to refresh.`;
        updatesEl.append(box);
      }
    } catch { /* updates are best-effort */ }
  }

  load();
  return wrap;
}
