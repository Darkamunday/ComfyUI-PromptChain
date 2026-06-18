import { mount, unmount } from "svelte";
import GeneratedGallery from "./components/gallery/GeneratedGallery.svelte";

export function mountGallery(target, props) {
  return mount(GeneratedGallery, { target, props });
}

export function destroyGallery(inst) {
  if (inst) unmount(inst);
}
