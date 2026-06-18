// Sidebar entry point — exports mount/destroy for the asset browser panel.

import { mount, unmount } from "svelte";
import AssetBrowser from "./components/sidebar/AssetBrowser.svelte";

let instance = null;

export function mountSidebar(target, props) {
  destroySidebar();
  instance = mount(AssetBrowser, { target, props });
  return instance;
}

export function destroySidebar() {
  if (instance) {
    unmount(instance);
    instance = null;
  }
}
