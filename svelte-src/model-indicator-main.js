// Model Indicator Svelte entry point — exports mount/destroy for picker and download modals.

import { mount, unmount } from "svelte";
import ModelPicker from "./components/picker/ModelPicker.svelte";
import DownloadModal from "./components/picker/DownloadModal.svelte";
import CatalogDownloadModal from "./components/picker/CatalogDownloadModal.svelte";
import NodePackInstallModal from "./components/picker/NodePackInstallModal.svelte";

export function mountModelPicker(target, props) {
  return mount(ModelPicker, { target, props });
}

export function destroyModelPicker(instance) {
  if (instance) unmount(instance);
}

export function mountDownloadModal(target, props) {
  return mount(DownloadModal, { target, props });
}

export function destroyDownloadModal(instance) {
  if (instance) unmount(instance);
}

export function mountCatalogDownloadModal(target, props) {
  return mount(CatalogDownloadModal, { target, props });
}

export function destroyCatalogDownloadModal(instance) {
  if (instance) unmount(instance);
}

export function mountNodePackInstallModal(target, props) {
  return mount(NodePackInstallModal, { target, props });
}

export function destroyNodePackInstallModal(instance) {
  if (instance) unmount(instance);
}
