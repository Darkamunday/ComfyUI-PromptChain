// Model Settings Svelte entry point — exports mount/destroy for the bridge.

import { mount, unmount } from "svelte";
import ModelSettingsModal from "./components/model/ModelSettingsModal.svelte";

export function mountModelSettings(target, props) {
  return mount(ModelSettingsModal, { target, props });
}

export function destroyModelSettings(instance) {
  if (instance) unmount(instance);
}
