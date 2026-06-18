<script>
  // EditorPane — CodeMirror 6 mount point.
  // Delegates editor creation to editor.js (pure logic). This component
  // manages the lifecycle (mount/destroy) and provides the container.

  let {
    node,
    shared,
  } = $props();

  let editorContainer;
  let editorView = null;

  // main.js creates the editor and mounts it into this component's container.
  // This component only manages the mount point lifecycle.

  export function getContainer() {
    return editorContainer;
  }

  export function setEditorView(view) {
    editorView = view;
  }
</script>

<div
  bind:this={editorContainer}
  class="pcr-editor-frame"
>
</div>
