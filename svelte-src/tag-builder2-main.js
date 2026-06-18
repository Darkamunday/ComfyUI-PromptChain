// Tag Builder v2 Svelte entry point — exports mount/destroy for the v2 bridge.

import { mount, unmount } from "svelte";
import TagBuilder2 from "./components/tag-builder2/TagBuilder2.svelte";

export function mountTagBuilder2(target, props) {
  return mount(TagBuilder2, { target, props });
}

export function destroyTagBuilder2(instance) {
  if (instance) unmount(instance);
}
