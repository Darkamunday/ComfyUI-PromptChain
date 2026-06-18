// Shared registry of live 3D Poser (PromptChain_PoseStudio) nodes.
//
// pose-studio.js populates this as Poser nodes mount/unmount; main.js reads it
// so each PromptChain node's menubar can gate the "pop the Poser into a panel"
// button (enabled only when a Poser exists) and so the panel knows which Poser
// to dock. A single shared module instead of a window global keeps it
// deterministic regardless of which extension file the loader runs first.
//
// Event-driven: subscribers are notified on add/remove (never polled).

const nodes = new Set();
const listeners = new Set();
let lastActive = null;

function notify() {
  for (const cb of listeners) {
    try { cb(); } catch (e) { console.error("[PromptChain] pose-registry listener error", e); }
  }
}

export const poseRegistry = {
  // Called by pose-studio.js on mount.
  add(node) {
    nodes.add(node);
    lastActive = node;
    notify();
  },
  // Called by pose-studio.js on teardown.
  remove(node) {
    const had = nodes.delete(node);
    if (lastActive === node) lastActive = null;
    if (had) notify();
  },
  // Called when a Poser viewport is interacted with, so "which Poser does the
  // panel dock" follows the one you last touched. Quiet by design — switching
  // focus shouldn't yank an already-open panel onto a different node.
  touch(node) {
    if (nodes.has(node)) lastActive = node;
  },
  // The viewport finished its async mount (Three.js loaded, _pcrPose with
  // enterDock now exists). add() fired its notify far earlier, before docking
  // was possible — so re-notify here, letting an already-open panel dock now.
  signalReady(node) {
    if (nodes.has(node)) notify();
  },
  // Every live Poser node — e.g. so a latent-size edit can offer itself to
  // each one (they individually re-trace whether that latent feeds them).
  all() {
    return [...nodes];
  },
  // The Poser a freshly-opened panel should host: last-interacted if still
  // alive, else any live one.
  getActive() {
    if (lastActive && lastActive._pcrAlive && nodes.has(lastActive)) return lastActive;
    for (const n of nodes) if (n._pcrAlive) return n;
    return null;
  },
  get count() { return nodes.size; },
  // Returns an unsubscribe fn. Fires on add/remove (gating + open-panel refresh).
  subscribe(cb) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  },
};
