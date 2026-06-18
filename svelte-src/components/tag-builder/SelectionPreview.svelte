<script>
  // SelectionPreview -- bottom bar showing accumulated selections as removable bubbles.

  const BUCKET_LABELS = {
    cast: "Cast",
    characters: "Char",
    appearance: "App",
    clothing: "Clothes",
    pose: "Pose",
    scene: "Scene",
    expression: "Expr",
    action: "Action",
    nsfw_action: "NSFW",
  };

  let { selections, onRemove = () => {}, onEdit = () => {} } = $props();

  let bubbles = $derived.by(() => {
    const result = [];

    // Characters -- pills per active dimension. A character can contribute 1–3
    // pills depending on which of base/outfit/pose are set. Removing the
    // CHARACTER pill just unchecks base, leaving outfit/pose intact.
    const characters = selections.characters || [];
    characters.forEach((char, charIdx) => {
      if (char.base) {
        result.push({
          type: "character",
          cssClass: "character",
          label: char.display,
          removeInfo: { bucket: "characters", index: charIdx, type: "character" },
        });
      }

      if (char.outfit) {
        result.push({
          type: "outfit",
          cssClass: "sub-item",
          label: char.outfit.display,
          removeInfo: { bucket: "characters", index: charIdx, type: "outfit" },
        });
      }

      if (char.pose) {
        result.push({
          type: "pose",
          cssClass: "sub-item",
          label: char.pose.display,
          removeInfo: { bucket: "characters", index: charIdx, type: "pose" },
        });
      }
    });

    // Props -- each prop as a bubble
    const props = selections.props || [];
    props.forEach((prop, propIdx) => {
      if (prop.display) {
        result.push({
          type: "Prop",
          cssClass: "props",
          label: prop.display,
          removeInfo: { bucket: "props", index: propIdx, type: "prop" },
        });
      }
    });

    // Other buckets
    for (const bucket of ["cast", "appearance", "clothing", "pose", "scene", "expression", "action", "nsfw_action"]) {
      const bucketSel = selections[bucket];
      if (!bucketSel) continue;
      for (const groupName in bucketSel) {
        const sel = bucketSel[groupName];
        const items = Array.isArray(sel) ? sel : [sel];
        for (const item of items) {
          if (item.display) {
            result.push({
              type: BUCKET_LABELS[bucket] || bucket,
              cssClass: "",
              label: item.display,
              removeInfo: { bucket, groupName, tag: item.tag, type: "mixer" },
            });
          }
        }
      }
    }

    return result;
  });
</script>

<div class="pcr-atb-selection">
  {#if bubbles.length === 0}
    <span class="pcr-atb-selection-empty">No selections</span>
  {:else}
    {#each bubbles as bubble, i}
      {@const editable = bubble.removeInfo.bucket === "characters"}
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div
        class="pcr-atb-bubble {bubble.cssClass}"
        class:editable
        onclick={editable ? () => onEdit(bubble.removeInfo) : null}
      >
        <span class="pcr-atb-bubble-type">{bubble.type}</span>
        <span class="pcr-atb-bubble-label" title={bubble.label}>{bubble.label}</span>
        <span class="pcr-atb-bubble-remove" onclick={(e) => { e.stopPropagation(); onRemove(bubble.removeInfo); }}>&times;</span>
      </div>
    {/each}
  {/if}
</div>
