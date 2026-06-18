// Svelte entry point — exports mount functions for vanilla JS integration.
// ComfyUI's app/api are passed in via the api bridge, not imported directly.

import { mount, unmount } from "svelte";
import ImageViewer from "./components/ImageViewer.svelte";

let viewerInstance = null;
let viewerContainer = null;

export function openImageViewer(target, props) {
  closeImageViewer();
  // release focus from CodeMirror / other inputs so keydown reaches window
  document.activeElement?.blur();
  viewerContainer = target;
  viewerInstance = mount(ImageViewer, { target, props });
  return () => closeImageViewer();
}

export function closeImageViewer() {
  if (viewerInstance) {
    unmount(viewerInstance);
    viewerInstance = null;
  }
  if (viewerContainer) {
    viewerContainer.remove();
    viewerContainer = null;
  }
}
