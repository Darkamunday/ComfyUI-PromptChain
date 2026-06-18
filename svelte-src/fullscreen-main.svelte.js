// Fullscreen editor entry point — exports mount/destroy.
// Tree data and API callbacks are passed in by the caller (fullscreen-editor.js or main.js).

import { mount, unmount } from "svelte";
import FullscreenEditor from "./components/fullscreen/FullscreenEditor.svelte";
import WildcardDropdown from "./components/shared/WildcardDropdown.svelte";

export class FullscreenState {
  // active node in the editor
  activeNodeId = $state(null);
  // tree data (computed by caller via tree-utils.js)
  treeRoots = $state([]);
  // open tabs
  openTabs = $state([]);
  activeWildcardTab = $state(null);
  // compiled output for active node's root
  compiledOutput = $state("");
  compiledNegOutput = $state("");
  // image preview
  imageUrl = $state(null);
  previewUrl = $state(null);
  progress = $state(null);
  isGenerating = $state(false);
  // sidebar view
  sidebarView = $state("edit");
  // editor font size
  fontSize = $state(13);
  // inline rename target
  renamingNodeId = $state(null);

  constructor() {}
}

export function mountFullscreen(target, props) {
  return mount(FullscreenEditor, { target, props });
}

export function destroyFullscreen(instance) {
  if (instance) unmount(instance);
}

// WildcardDropdown — imperative mount/unmount for use from bridge code
let wcDropdownInstance = null;
let wcDropdownTarget = null;

export function showWildcardDropdown(props) {
  hideWildcardDropdown();
  wcDropdownTarget = document.createElement("div");
  document.body.appendChild(wcDropdownTarget);
  const originalClose = props.onClose;
  props.onClose = () => {
    originalClose?.();
    hideWildcardDropdown();
  };
  wcDropdownInstance = mount(WildcardDropdown, { target: wcDropdownTarget, props });
}

export function hideWildcardDropdown() {
  if (wcDropdownInstance) { unmount(wcDropdownInstance); wcDropdownInstance = null; }
  if (wcDropdownTarget) { wcDropdownTarget.remove(); wcDropdownTarget = null; }
}
