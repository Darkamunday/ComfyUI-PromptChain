// Documents — multi-prompt storage per node.
// Each node can have multiple named prompts saved to node.properties.
//
// Stored in node.properties:
//   pcrDocuments:  [{ id, name, content, contentHash, lastOpened, lastModified }]
//   pcrActiveDocId: ID of the currently loaded document
//
// The dropdown appears in the footer left side. Auto-saves on edit (500ms debounce).

import { app } from "../../../scripts/app.js";
import { setEditorContent } from "./editor.js";

function hashContent(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash |= 0;
  }
  return hash;
}

function formatRelativeTime(timestamp) {
  if (!timestamp) return null;
  const diff = Date.now() - timestamp;
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  if (mins < 1) return "now";
  if (mins < 60) return mins + "m";
  if (hours < 24) return hours + "h";
  if (days < 365) return days + "d";
  return Math.floor(days / 365) + "y";
}

// simple global: pointerdown outside closes any open doc menu.
// capture phase fires before isolation.js stopPropagation on the container.
let activeCloseCallback = null;
document.addEventListener("pointerdown", (e) => {
  if (!activeCloseCallback) return;
  if (e.target.closest(".pcr-doc-menu, .pcr-doc-dropdown")) return;
  activeCloseCallback();
  activeCloseCallback = null;
}, true);

export function createDocumentDropdown(node) {
  // -- storage helpers --
  const getDocs = () => {
    if (!node.properties) node.properties = {};
    if (!node.properties.pcrDocuments) node.properties.pcrDocuments = [];
    return node.properties.pcrDocuments;
  };
  const saveDocs = (docs) => {
    if (!node.properties) node.properties = {};
    node.properties.pcrDocuments = docs;
    app.graph?.setDirtyCanvas?.(true, true);
  };
  const getActiveId = () => node.properties?.pcrActiveDocId || null;
  const setActiveId = (id) => {
    if (!node.properties) node.properties = {};
    node.properties.pcrActiveDocId = id;
  };
  const nextId = () => {
    const docs = getDocs();
    return docs.length === 0 ? 1 : docs.reduce((max, d) => d.id > max ? d.id : max, 0) + 1;
  };
  // Resolve a desired name to one not already used by any doc (excluding excludeId).
  // Collisions are suffixed " (n)" with the lowest free n ≥ 2; an existing
  // " (n)" tail on `desired` is stripped first so "foo (2)" + collision → "foo (3)",
  // not "foo (2) (2)".
  const uniqueName = (desired, excludeId = null) => {
    const used = new Set(getDocs().filter(d => d.id !== excludeId).map(d => d.name));
    if (!used.has(desired)) return desired;
    const m = desired.match(/^(.*) \((\d+)\)$/);
    const base = m ? m[1] : desired;
    let n = 2;
    while (used.has(`${base} (${n})`)) n++;
    return `${base} (${n})`;
  };
  const getEditorText = () => node._pcrEditor?.state.doc.toString() || "";
  const setEditorText = (text) => {
    if (!node._pcrEditor) return;
    setEditorContent(node._pcrEditor, text);
    // sync to hidden prompt widget
    const promptWidget = node.widgets?.find(w => w.name === "prompt");
    if (promptWidget) promptWidget.value = text;
  };

  // -- DOM --
  const dropdown = document.createElement("div");
  dropdown.className = "pcr-doc-dropdown";

  const label = document.createElement("span");
  label.className = "pcr-doc-label";
  label.textContent = "Untitled";
  dropdown.appendChild(label);

  const arrow = document.createElement("span");
  arrow.className = "pcr-doc-arrow";
  arrow.textContent = "▾";
  dropdown.appendChild(arrow);

  const menu = document.createElement("div");
  menu.className = "pcr-doc-menu";

  // -- state --
  let currentDocId = null;
  let menuOpen = false;
  let autoSaveTimeout = null;
  let menuAppended = false;

  // -- menu items --
  function createMenuItem(doc, isActive, allDocs) {
    const item = document.createElement("div");
    item.className = "pcr-doc-item" + (isActive ? " pcr-doc-item-active" : "");

    const nameSpan = document.createElement("span");
    nameSpan.className = "pcr-doc-item-name";
    nameSpan.textContent = doc.name;
    item.appendChild(nameSpan);

    const right = document.createElement("div");
    right.className = "pcr-doc-item-right";

    const time = formatRelativeTime(doc.lastModified || doc.lastOpened);
    if (time) {
      const timeSpan = document.createElement("span");
      timeSpan.className = "pcr-doc-item-time";
      timeSpan.textContent = time;
      right.appendChild(timeSpan);
    }

    if (isActive) {
      const editBtn = document.createElement("button");
      editBtn.className = "pcr-doc-item-btn";
      editBtn.textContent = "✎";
      editBtn.title = "Rename";
      editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        startRename(nameSpan, doc);
      });
      right.appendChild(editBtn);
    } else if (allDocs.length > 1) {
      const delBtn = document.createElement("button");
      delBtn.className = "pcr-doc-item-btn pcr-doc-item-delete";
      delBtn.textContent = "✕";
      delBtn.title = "Delete";
      delBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteDoc(doc.id, doc.name);
      });
      right.appendChild(delBtn);
    }

    item.appendChild(right);

    if (!isActive) {
      item.addEventListener("click", () => { switchDoc(doc.id); closeMenu(); });
    }
    return item;
  }

  function populateMenu() {
    menu.innerHTML = "";
    const docs = getDocs();

    const list = document.createElement("div");
    list.className = "pcr-doc-list";

    const active = docs.find(d => d.id === currentDocId);
    if (active) list.appendChild(createMenuItem(active, true, docs));

    const others = docs.filter(d => d.id !== currentDocId)
      .sort((a, b) => (b.lastOpened || 0) - (a.lastOpened || 0));
    for (const doc of others) list.appendChild(createMenuItem(doc, false, docs));

    menu.appendChild(list);

    const newItem = document.createElement("div");
    newItem.className = "pcr-doc-item pcr-doc-item-new";
    newItem.innerHTML = `<span style="color: #4fc3f7;">+ New</span>`;
    newItem.addEventListener("click", (e) => { e.stopPropagation(); createDoc(); });
    menu.appendChild(newItem);
  }

  // -- open/close --
  function closeMenu() {
    if (!menuOpen) return;
    menu.style.display = "none";
    menuOpen = false;
    if (activeCloseCallback === closeMenu) activeCloseCallback = null;
  }

  function openMenu() {
    if (activeCloseCallback && activeCloseCallback !== closeMenu) activeCloseCallback();
    if (!menuAppended) { document.body.appendChild(menu); menuAppended = true; }
    populateMenu();
    const rect = dropdown.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.left = `${rect.left}px`;
    menu.style.top = `${rect.bottom + 4}px`;
    menu.style.bottom = "auto";
    menu.style.display = "block";
    menuOpen = true;
    activeCloseCallback = closeMenu;
  }

  dropdown.addEventListener("click", (e) => {
    e.stopPropagation();
    menuOpen ? closeMenu() : openMenu();
  });

  // -- rename --
  function startRename(nameSpan, doc) {
    const original = doc.name;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "pcr-doc-rename-input";
    input.value = doc.name;
    nameSpan.textContent = "";
    nameSpan.appendChild(input);
    requestAnimationFrame(() => { input.focus(); input.select(); });

    const finish = () => {
      renameDoc(doc.id, input.value.trim() || original);
      closeMenu();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(); }
      else if (e.key === "Escape") { e.preventDefault(); nameSpan.textContent = original; closeMenu(); }
      e.stopPropagation();
    });
    input.addEventListener("blur", finish);
    input.addEventListener("click", (e) => e.stopPropagation());
  }

  // -- operations --
  function saveCurrent() {
    if (!currentDocId) return;
    const docs = getDocs();
    const doc = docs.find(d => d.id === currentDocId);
    if (!doc) return;
    const content = getEditorText();
    const newHash = hashContent(content);
    if (doc.contentHash !== newHash) {
      doc.content = content;
      doc.contentHash = newHash;
      doc.lastModified = Date.now();
      saveDocs(docs);
    }
  }

  function createDoc() {
    saveCurrent();
    const content = getEditorText();
    const docs = getDocs();
    const baseName = docs.find(d => d.id === currentDocId)?.name || "Untitled";
    const newDoc = {
      id: nextId(), name: uniqueName(baseName), content,
      contentHash: hashContent(content),
      lastOpened: Date.now(), lastModified: Date.now(),
    };
    docs.push(newDoc);
    saveDocs(docs);
    currentDocId = newDoc.id;
    setActiveId(newDoc.id);
    label.textContent = newDoc.name;
    populateMenu();
    const activeItem = menu.querySelector(".pcr-doc-item-active");
    const nameSpan = activeItem?.querySelector(".pcr-doc-item-name");
    if (nameSpan) startRename(nameSpan, newDoc);
  }

  function switchDoc(docId) {
    saveCurrent();
    const docs = getDocs();
    const doc = docs.find(d => d.id === docId);
    if (!doc) return;
    doc.lastOpened = Date.now();
    saveDocs(docs);
    currentDocId = docId;
    setActiveId(docId);
    label.textContent = doc.name;
    setEditorText(doc.content);
    node._pcrEditor?.focus();
  }

  function renameDoc(docId, name) {
    const docs = getDocs();
    const doc = docs.find(d => d.id === docId);
    if (!doc) return;
    const resolved = uniqueName(name, docId);
    doc.name = resolved;
    saveDocs(docs);
    if (docId === currentDocId) label.textContent = resolved;
  }

  function deleteDoc(docId, name) {
    if (!confirm(`Delete "${name}"?`)) return;
    const docs = getDocs();
    if (docs.length <= 1) return;
    const idx = docs.findIndex(d => d.id === docId);
    if (idx === -1) return;
    docs.splice(idx, 1);
    saveDocs(docs);
    if (docId === currentDocId && docs[0]) switchDoc(docs[0].id);
    populateMenu();
  }

  // -- init --
  function init() {
    const docs = getDocs();
    const content = getEditorText();
    let activeId = getActiveId();

    if (docs.length === 0) {
      const newDoc = {
        id: nextId(), name: "Untitled", content,
        contentHash: hashContent(content),
        lastOpened: Date.now(), lastModified: Date.now(),
      };
      docs.push(newDoc);
      saveDocs(docs);
      activeId = newDoc.id;
      setActiveId(activeId);
    }

    let activeDoc = docs.find(d => d.id === activeId) || docs[0];
    if (activeDoc) {
      currentDocId = activeDoc.id;
      setActiveId(activeDoc.id);
      label.textContent = activeDoc.name;
      if (activeDoc.content !== content) {
        activeDoc.content = content;
        activeDoc.contentHash = hashContent(content);
        saveDocs(docs);
      }
    }
  }

  // defer init until editor is ready
  requestAnimationFrame(() => requestAnimationFrame(init));

  return {
    element: dropdown,
    saveCurrent,
    scheduleAutoSave() {
      clearTimeout(autoSaveTimeout);
      autoSaveTimeout = setTimeout(saveCurrent, 500);
    },
    cleanup() {
      clearTimeout(autoSaveTimeout);
      closeMenu();
      if (menuAppended) { menu.remove(); menuAppended = false; }
    },
  };
}
