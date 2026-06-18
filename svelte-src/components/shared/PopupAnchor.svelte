<script>
  // PopupAnchor — body-level popup portal with viewport-aware positioning.
  // Moves itself to document.body on mount to escape node overflow clipping.
  // Only one popup can be open at a time (singleton via node-stores).

  import { onMount, onDestroy, tick } from "svelte";
  import {
    closeActivePopup,
    openPopupState,
    popup,
  } from "../../lib/node-stores.svelte.js";

  let {
    triggerRect = null,
    popupKey = "",
    triggerEl = null,
    onClose = () => {},
    children,
  } = $props();

  let menuEl;
  let openedAt = 0;

  function close() {
    onClose();
  }

  // Dismiss listens at CAPTURE phase so panel-level stopPropagation
  // handlers (like .pcr-ai-panel's onclick stop) don't eat the event
  // before we see it. Skip if the target is inside the menu OR inside
  // the trigger button — the trigger has its own toggle handler that
  // would otherwise race with dismiss.
  function dismiss(e) {
    if (Date.now() - openedAt < 300) return;
    if (menuEl?.contains(e.target)) return;
    if (triggerEl?.contains(e.target)) return;
    close();
  }

  function reposition() {
    if (!menuEl || !triggerRect) return;
    requestAnimationFrame(() => {
      if (!menuEl) return;
      const menuRect = menuEl.getBoundingClientRect();
      let left = triggerRect.left;
      let top = triggerRect.bottom + 4;
      if (left + menuRect.width > window.innerWidth) left = window.innerWidth - menuRect.width - 10;
      if (top + menuRect.height > window.innerHeight) top = triggerRect.top - menuRect.height - 4;
      if (left < 10) left = 10;
      if (top < 10) top = 10;
      menuEl.style.left = `${left}px`;
      menuEl.style.top = `${top}px`;
    });
  }

  onMount(() => {
    // portal: move to document.body to escape overflow clipping
    document.body.appendChild(menuEl);
    openedAt = Date.now();
    openPopupState(popupKey, close);

    // Capture phase so the panel's onclick stop-propagation can't
    // shield clicks outside the menu from us.
    document.addEventListener("click", dismiss, true);
    document.addEventListener("pointerdown", dismiss, true);

    reposition();
  });

  onDestroy(() => {
    document.removeEventListener("click", dismiss, true);
    document.removeEventListener("pointerdown", dismiss, true);
    if (menuEl?.parentNode === document.body) menuEl.remove();
    // Clear the popup singleton if WE are still the active popup so the
    // next instance's openPopupState doesn't fire our (now-stale) close
    // and immediately self-close. Other instances clear their own slot.
    if (popup.activeKey === popupKey) {
      popup.activeKey = null;
      popup.close = null;
    }
  });

  // reposition when triggerRect changes
  $effect(() => {
    if (triggerRect) reposition();
  });
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  bind:this={menuEl}
  class="pcr-mode-menu"
  style="position:fixed;z-index:100000;"
  onclick={(e) => e.stopPropagation()}
  onpointerdown={(e) => e.stopPropagation()}
>
  {@render children()}
</div>
