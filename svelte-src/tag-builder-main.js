// Tag Builder Svelte entry point — exports mount/destroy for the bridge.

import { mount, unmount } from "svelte";
import TagBuilder from "./components/tag-builder/TagBuilder.svelte";

export function mountTagBuilder(target, props) {
  return mount(TagBuilder, { target, props });
}

export function destroyTagBuilder(instance) {
  if (instance) unmount(instance);
}
