<script>
  // TagBuilder -- main panel component for the structured prompt composer.
  // Manages tab state, data loading, selections, and modal lifecycle.

  import { untrack, onDestroy } from "svelte";
  import { buildInsertText } from "../../lib/tag-builder-utils.js";
  import { MULTI_SELECT_GROUPS, CUSTOMIZABLE_CLOTHING_GROUPS } from "../../lib/tag-builder-constants.js";
  import MixerGrid from "./MixerGrid.svelte";
  import SelectionPreview from "./SelectionPreview.svelte";
  import CharacterModal from "./CharacterModal.svelte";
  import PropsCustomizerModal from "./PropsCustomizerModal.svelte";
  import NsfwActionModal from "./NsfwActionModal.svelte";
  import ClothingCustomizer from "./ClothingCustomizer.svelte";
  import FantasyCustomizer from "./FantasyCustomizer.svelte";
  import ClothingPanel from "./ClothingPanel.svelte";

  const TABS = [
    { key: "all", label: "All", icon: "\uD83D\uDD0D" },
    { key: "cast", label: "Cast", icon: "\uD83D\uDC65" },
    { key: "characters", label: "Character", icon: "\uD83D\uDC64" },
    { key: "appearance", label: "Appearance", icon: "\u2728" },
    { key: "clothing", label: "Clothing", icon: "\uD83D\uDC55" },
    { key: "pose", label: "Pose", icon: "\uD83E\uDDD8" },
    { key: "props", label: "Props", icon: "\uD83D\uDECB\uFE0F" },
    { key: "expression", label: "Expression", icon: "\uD83D\uDE0A" },
    { key: "action", label: "Action", icon: "\u26A1" },
    { key: "scene", label: "Scene", icon: "\uD83C\uDFE0" },
  ];

  const ALL_TAB_BUCKETS = ["characters", "appearance", "clothing", "pose", "props", "scene", "expression", "action", "nsfw_action"];

  const BUCKET_INFO = {
    characters: { label: "Characters", icon: "\uD83D\uDC64" },
    appearance: { label: "Appearance", icon: "\u2728" },
    clothing: { label: "Clothing", icon: "\uD83D\uDC55" },
    pose: { label: "Pose", icon: "\uD83E\uDDD8" },
    props: { label: "Props", icon: "\uD83D\uDECB\uFE0F" },
    scene: { label: "Scene", icon: "\uD83C\uDFE0" },
    expression: { label: "Expression", icon: "\uD83D\uDE0A" },
    action: { label: "Action", icon: "\u26A1" },
    nsfw_action: { label: "NSFW", icon: "\uD83D\uDD1E" },
  };

  let {
    from = 0,
    to = 0,
    initialTab = "all",
    initialQuery = "",
    tagSourceConfig = {},
    onPromptStyleChange = () => {},
    onInsert = () => {},
    onClose = () => {},
  } = $props();

  // --- State ---
  let activeTab = $state(initialTab);
  let searchQuery = $state(initialQuery);
  // Initial mode comes from resolved tag config (workflow override → model default → "tags").
  // Toggling writes back via onPromptStyleChange so the choice persists per workflow.
  let isNaturalMode = $state(tagSourceConfig.prompt_style === "natural");
  let loading = $state(false);
  let selections = $state({
    cast: {},
    characters: [],
    appearance: {},
    clothing: {},
    pose: {},
    props: [],
    scene: {},
    expression: {},
    action: {},
    nsfw_action: {},
  });
  let cache = $state({});

  // Panel position

  // Search debounce timeout handle.  Using onDestroy because the
  // `$effect(() => () => cleanup)` pattern doesn't invoke the inner
  // teardown (no tracked deps in the outer function).
  let searchTimeout = null;

  onDestroy(() => { clearTimeout(searchTimeout); });

  // Tab-specific loaded data
  let tabData = $state({});

  // Modal states
  let showCharacterModal = $state(false);
  let characterModalTag = $state("");
  let characterModalDisplay = $state("");

  let showPropsModal = $state(false);
  let propsModalCategory = $state("");
  let propsModalPreSelected = $state(null);

  let showNsfwModal = $state(false);
  let nsfwModalGroups = $state([]);

  let showClothingCustomizer = $state(false);
  let clothingCustomizerItem = $state(null);

  let showFantasyCustomizer = $state(false);
  let fantasyCustomizerItem = $state(null);

  // Character categories for the characters tab
  let characterCategories = $state([]);

  // "All" tab grouped data
  let allTabGroups = $state({});

  // --- Search element ref ---
  let searchInputEl;


  // --- Escape key handler ---
  // Single owner of Escape at the panel level: peel off the innermost layer first
  // (open dropdown → sub-modal → panel). Dropdown key handlers intentionally don't
  // touch Escape because setting state there would race this handler.
  $effect(() => {
    function onKeydown(e) {
      if (e.key !== "Escape") return;
      if (openDropdownKey) {
        openDropdownKey = null;
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (showCharacterModal || showPropsModal || showNsfwModal || showClothingCustomizer || showFantasyCustomizer) return;
      onClose();
    }
    document.addEventListener("keydown", onKeydown);
    return () => document.removeEventListener("keydown", onKeydown);
  });

  // --- Load initial tab on mount (untracked to avoid re-running on state changes) ---
  $effect(() => {
    untrack(() => {
      loadTabContent(activeTab, searchQuery, true);
      if (searchInputEl) searchInputEl.focus();
    });
  });

  // --- Debounced search ---
  function handleSearchInput() {
    if (searchTimeout) clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      loadTabContent(activeTab, searchQuery, false);
    }, 200);
  }

  function clearSearch() {
    searchQuery = "";
    loadTabContent(activeTab, "", false);
    searchInputEl?.focus();
  }

  // --- Tab switching ---
  function switchTab(tabKey) {
    if (tabKey === activeTab) {
      if (tabKey === "all") return;
      tabKey = "all";
    }
    activeTab = tabKey;
    searchQuery = "";
    loadTabContent(tabKey, "", true);
  }

  // --- Data loading ---
  async function loadTabContent(tab, query, showSpinner) {
    if (showSpinner) loading = true;
    try {
      if (tab === "all") {
        await loadAllContent(query);
      } else if (tab === "characters") {
        await loadCharacterContent(query);
      } else if (tab === "props") {
        await loadPropsContent(query);
      } else {
        await loadMixerContent(tab, query);
      }
    } catch (e) {
      console.error(`[TagBuilder] Failed to load ${tab}:`, e);
    }
    loading = false;
  }

  async function loadMixerContent(bucket, query) {
    if (!cache[bucket]) {
      const [groupsRes, itemsRes] = await Promise.all([
        fetch(`/promptchain/tag-builder/buckets/${bucket}/groups`),
        fetch(`/promptchain/tag-builder/buckets/${bucket}/items`),
      ]);
      if (!groupsRes.ok || !itemsRes.ok) throw new Error("Failed to fetch");

      const groupsData = await groupsRes.json();
      const itemsData = await itemsRes.json();

      const itemsByGroup = {};
      for (const item of itemsData.items) {
        if (!itemsByGroup[item.item_group]) itemsByGroup[item.item_group] = [];
        itemsByGroup[item.item_group].push({
          tag: item.item_tag,
          display: item.display_name,
          tags: item.base_tags,
          natlang: item.base_natlang,
          group: item.item_group,
        });
      }

      cache[bucket] = groupsData.groups.map(g => ({
        name: g.group_name,
        display: g.display_name,
        items: itemsByGroup[g.group_name] || [],
      }));
    }
    tabData[bucket] = cache[bucket];
  }

  async function loadCharacterContent(query) {
    let url = "/promptchain/tag-builder/character-categories";
    if (query) url += `?search=${encodeURIComponent(query)}`;

    const response = await fetch(url);
    if (!response.ok) throw new Error("Failed to fetch");
    const data = await response.json();
    characterCategories = data.categories || [];
  }

  async function loadPropsContent(query) {
    if (!cache.propsData) {
      const response = await fetch("/promptchain/props/all");
      if (!response.ok) throw new Error("Failed to fetch props data");
      cache.propsData = await response.json();
    }
    // Props data is cached in cache.propsData; rendering uses it directly
  }

  async function loadAllContent(query) {
    const lowerQuery = (query || "").toLowerCase();
    const dataPromises = [];

    // Characters
    let charUrl = "/promptchain/tag-builder/character-categories";
    if (lowerQuery) charUrl += `?search=${encodeURIComponent(lowerQuery)}`;
    dataPromises.push(
      fetch(charUrl)
        .then(r => r.ok ? r.json() : { categories: [] })
        .then(data => ({ bucket: "characters", categories: data.categories || [] }))
        .catch(() => ({ bucket: "characters", categories: [] }))
    );

    // Props
    if (!cache.propsData) {
      dataPromises.push(
        fetch("/promptchain/props/all")
          .then(r => r.ok ? r.json() : null)
          .then(data => {
            if (data) cache.propsData = data;
            return { bucket: "props", propsData: data };
          })
          .catch(() => ({ bucket: "props", propsData: null }))
      );
    } else {
      dataPromises.push(Promise.resolve({ bucket: "props", propsData: cache.propsData }));
    }

    // Other mixer buckets
    const mixerBuckets = ["appearance", "clothing", "pose", "scene", "expression", "action", "nsfw_action"];
    for (const bucket of mixerBuckets) {
      if (!cache[bucket]) {
        dataPromises.push(
          Promise.all([
            fetch(`/promptchain/tag-builder/buckets/${bucket}/groups`).then(r => r.ok ? r.json() : { groups: [] }),
            fetch(`/promptchain/tag-builder/buckets/${bucket}/items`).then(r => r.ok ? r.json() : { items: [] }),
          ]).then(([groupsData, itemsData]) => {
            const itemsByGroup = {};
            for (const item of itemsData.items) {
              if (!itemsByGroup[item.item_group]) itemsByGroup[item.item_group] = [];
              itemsByGroup[item.item_group].push({
                tag: item.item_tag,
                display: item.display_name,
                tags: item.base_tags,
                natlang: item.base_natlang,
                group: item.item_group,
              });
            }
            cache[bucket] = groupsData.groups.map(g => ({
              name: g.group_name,
              display: g.display_name,
              items: itemsByGroup[g.group_name] || [],
            }));
            return { bucket, groups: cache[bucket] };
          }).catch(() => ({ bucket, groups: [] }))
        );
      } else {
        dataPromises.push(Promise.resolve({ bucket, groups: cache[bucket] }));
      }
    }

    const results = await Promise.all(dataPromises);
    const grouped = {};

    for (const result of results) {
      if (result.bucket === "characters") {
        const charGroups = [];
        for (const category of result.categories) {
          const items = [];
          for (const series of category.series) {
            for (const char of series.characters) {
              items.push({
                tag: char.tag,
                display: char.display || char.tag,
                series: series.name,
              });
            }
          }
          if (items.length > 0) {
            charGroups.push({
              name: `_char_${category.tag}`,
              display: category.name,
              bucket: "characters",
              category: category.tag,
              items,
              seriesData: category.series,
            });
          }
        }
        grouped.characters = charGroups;
      } else if (result.bucket === "props" && result.propsData) {
        const propGroups = [];
        for (const cat of result.propsData.categories || []) {
          const categoryProps = (result.propsData.props || []).filter(p => p.category === cat.category);
          if (categoryProps.length > 0) {
            propGroups.push({
              name: `_props_${cat.category}`,
              display: `${cat.icon} ${cat.display_name}`,
              bucket: "props",
              category: cat.category,
              items: categoryProps.map(p => ({
                tag: p.prop_tag,
                display: p.display_name || p.prop_tag,
                category: p.category,
                is_customizable: p.is_customizable,
              })),
            });
          }
        }
        grouped.props = propGroups;
      } else if (result.groups) {
        grouped[result.bucket] = result.groups.map(g => ({ ...g, bucket: result.bucket }));
      }
    }

    allTabGroups = grouped;
  }

  // --- All-tab filtered groups per bucket ---
  function getAllFilteredGroups(bucket) {
    const groups = allTabGroups[bucket] || [];
    const lowerQuery = (searchQuery || "").toLowerCase();
    if (!lowerQuery) return groups;

    return groups
      .map(group => {
        const filtered = group.items.filter(item => {
          const normalize = s => (s || "").toLowerCase().replace(/[_\s]+/g, " ");
          const q = normalize(lowerQuery);
          return normalize(item.display || item.tag).includes(q) ||
                 normalize(item.tags || "").includes(q) ||
                 normalize(item.series || "").includes(q);
        });
        return { ...group, items: filtered };
      })
      .filter(g => g.items.length > 0);
  }

  // --- Selection handlers ---
  function handleMixerSelect(bucket, groupName, item, mode) {
    if (mode === "clear") {
      delete selections[bucket][groupName];
      selections = { ...selections };
      return;
    }

    if (!selections[bucket]) selections[bucket] = {};

    if (mode === "toggle") {
      let arr = selections[bucket][groupName];
      if (!Array.isArray(arr)) arr = [];

      const idx = arr.findIndex(s => s.tag === item.tag);
      if (idx >= 0) {
        arr.splice(idx, 1);
      } else {
        arr.push({ tag: item.tag, tags: item.tags, natlang: item.natlang, display: item.display || item.tag });
      }

      if (arr.length > 0) {
        selections[bucket][groupName] = arr;
      } else {
        delete selections[bucket][groupName];
      }
    } else {
      // single select
      if (item) {
        selections[bucket][groupName] = {
          tag: item.tag,
          tags: item.tags,
          natlang: item.natlang,
          display: item.display || item.tag,
        };
      } else {
        delete selections[bucket][groupName];
      }
    }
    selections = { ...selections };
  }

  // --- All tab: character click opens character modal ---
  function handleAllCharacterClick(tag, display) {
    characterModalTag = tag;
    characterModalDisplay = display;
    showCharacterModal = true;
  }

  // --- All tab: props click opens props modal ---
  function handleAllPropsClick(category, preSelectedTag) {
    propsModalCategory = category;
    propsModalPreSelected = preSelectedTag;
    showPropsModal = true;
  }

  // --- All tab: selection handler for mixed buckets ---
  function handleAllSelect(bucket, groupName, item, mode) {
    if (bucket === "characters") {
      if (!item) {
        selections.characters = [];
        selections = { ...selections };
      } else {
        handleAllCharacterClick(item.tag, item.display || item.tag);
      }
      return;
    }

    if (bucket === "props") {
      if (!item) {
        // Clear props in this category
        const category = groupName.replace("_props_", "");
        selections.props = (selections.props || []).filter(p => p.category !== category);
        selections = { ...selections };
      } else {
        handleAllPropsClick(item.category || groupName.replace("_props_", ""), item.tag);
      }
      return;
    }

    // Check for clothing customizer
    if (bucket === "clothing" && CUSTOMIZABLE_CLOTHING_GROUPS.includes(groupName.toLowerCase()) && item) {
      handleOpenClothingCustomizer({
        tag: item.tag,
        display: item.display || item.tag,
        tags: item.tags,
        natlang: item.natlang,
        group: groupName.toLowerCase(),
      });
      return;
    }

    // Check for fantasy customizer
    if (bucket === "appearance" && groupName.toLowerCase() === "fantasy" && item) {
      handleOpenFantasyCustomizer({
        tag: item.tag,
        display: item.display || item.tag,
        tags: item.tags,
        natlang: item.natlang,
      });
      return;
    }

    handleMixerSelect(bucket, groupName, item, mode);
  }

  // --- Character modal ---
  // If the character already exists in selections, replace it in place; otherwise
  // append. Lets the modal also serve as an "edit" surface for selected characters.
  function handleCharacterSelected(charObj) {
    const chars = selections.characters || [];
    const idx = chars.findIndex(c => c.tag === charObj.tag);
    selections.characters = idx >= 0
      ? chars.map((c, i) => i === idx ? charObj : c)
      : [...chars, charObj];
    showCharacterModal = false;
  }

  // --- Unified dropdown state ---
  // Single source of truth so opening any dropdown (char category, props category,
  // or MixerGrid group) automatically closes whichever one was previously open.
  // Key format: "char:<tag>", "props:<name>", "mixer:<bucket>:<group>".
  let openDropdownKey = $state(null);
  // In-dropdown filter: query replaces the header label when a dropdown is open,
  // filters items within that dropdown only. Cleared on open/close transitions.
  let dropdownSearchQuery = $state("");
  // Keyboard-nav highlight: first match is always highlighted so Enter has a target.
  let highlightedIndex = $state(0);

  function setOpenDropdown(key) {
    openDropdownKey = openDropdownKey === key ? null : key;
  }

  $effect(() => {
    openDropdownKey;
    untrack(() => { dropdownSearchQuery = ""; highlightedIndex = 0; });
  });
  $effect(() => {
    dropdownSearchQuery;
    untrack(() => { highlightedIndex = 0; });
  });

  function focusOnMount(node) { node.focus(); }

  // Auto-scrolls the highlighted option into view when highlightedIndex lands on it.
  function highlightScroll(node, highlighted) {
    function check(h) { if (h) node.scrollIntoView({ block: "nearest" }); }
    check(highlighted);
    return { update: check };
  }

  function handleDropdownKey(e, flatList, selectFn) {
    // Escape is owned by the document-level handler so it doesn't cascade to the panel.
    if (e.key === "ArrowDown") { e.preventDefault(); highlightedIndex = Math.min(Math.max(flatList.length - 1, 0), highlightedIndex + 1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); highlightedIndex = Math.max(0, highlightedIndex - 1); return; }
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); const item = flatList[highlightedIndex]; if (item) selectFn(item); return; }
  }

  function filterDropdownItems(items, getDisplay, getTag) {
    const q = (dropdownSearchQuery || "").toLowerCase().replace(/[_\s]+/g, " ").trim();
    if (!q) return items;
    return items.filter(item => {
      const d = (getDisplay(item) || "").toLowerCase().replace(/[_\s]+/g, " ");
      const t = (getTag(item) || "").toLowerCase().replace(/[_\s]+/g, " ");
      return d.includes(q) || t.includes(q);
    });
  }

  function mixerOpenGroupFor(bucket) {
    const prefix = `mixer:${bucket}:`;
    return openDropdownKey?.startsWith(prefix) ? openDropdownKey.slice(prefix.length) : null;
  }

  function setMixerOpenGroup(bucket, group) {
    openDropdownKey = group ? `mixer:${bucket}:${group}` : null;
  }

  function toggleCharCategory(categoryTag) {
    setOpenDropdown(`char:${categoryTag}`);
  }

  $effect(() => {
    if (!openDropdownKey) return;
    function onDocClick(e) {
      if (e.target.closest(".pcr-atb-mixer-group.open")) return;
      openDropdownKey = null;
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  });

  function handleCharOptionClick(tag, display) {
    openDropdownKey = null;
    characterModalTag = tag;
    characterModalDisplay = display;
    showCharacterModal = true;
  }

  // --- Props tab: category buttons ---
  function handlePropsCategoryClick(category) {
    propsModalCategory = category;
    propsModalPreSelected = null;
    showPropsModal = true;
  }

  function handlePropConfirm(propObj) {
    if (!selections.props) selections.props = [];
    selections.props = [...selections.props, propObj];
    showPropsModal = false;
  }

  function removeProp(index) {
    selections.props = selections.props.filter((_, i) => i !== index);
  }

  // --- Props search grid ---
  let propsSearchGroups = $derived.by(() => {
    if (!cache.propsData || activeTab !== "props") return [];
    const lowerQuery = (searchQuery || "").toLowerCase().replace(/[_\s]+/g, " ");
    if (!lowerQuery) return [];

    const result = [];
    for (const cat of cache.propsData.categories || []) {
      const categoryProps = (cache.propsData.props || []).filter(p => p.category === cat.category);
      const filtered = categoryProps.filter(p => {
        const d = (p.display_name || p.prop_tag).toLowerCase().replace(/[_\s]+/g, " ");
        const t = (p.prop_tag || "").toLowerCase().replace(/[_\s]+/g, " ");
        return d.includes(lowerQuery) || t.includes(lowerQuery);
      });
      if (filtered.length > 0) {
        result.push({ category: cat, props: filtered });
      }
    }
    return result;
  });

  // --- NSFW Action Modal ---
  async function handleOpenNsfwModal() {
    // Load NSFW action data if not cached
    if (!cache.nsfw_action) {
      try {
        const [groupsRes, itemsRes] = await Promise.all([
          fetch("/promptchain/tag-builder/buckets/nsfw_action/groups"),
          fetch("/promptchain/tag-builder/buckets/nsfw_action/items"),
        ]);
        if (!groupsRes.ok || !itemsRes.ok) throw new Error("Failed to fetch");

        const groupsData = await groupsRes.json();
        const itemsData = await itemsRes.json();

        const itemsByGroup = {};
        for (const item of itemsData.items) {
          if (!itemsByGroup[item.item_group]) itemsByGroup[item.item_group] = [];
          itemsByGroup[item.item_group].push({
            tag: item.item_tag,
            display: item.display_name,
            tags: item.base_tags,
            natlang: item.base_natlang,
          });
        }

        cache.nsfw_action = groupsData.groups.map(g => ({
          name: g.group_name,
          display: g.display_name,
          items: itemsByGroup[g.group_name] || [],
        }));
      } catch (e) {
        console.error("[TagBuilder] Failed to load NSFW actions:", e);
        return;
      }
    }

    nsfwModalGroups = cache.nsfw_action;
    showNsfwModal = true;
  }

  function handleNsfwConfirm(pendingSelections) {
    selections.nsfw_action = pendingSelections;
    selections = { ...selections };
    showNsfwModal = false;
    // Refresh action tab data if currently on action tab
    if (activeTab === "action") {
      loadTabContent("action", searchQuery);
    }
  }

  // --- Clothing Customizer ---
  function handleOpenClothingCustomizer(itemInfo) {
    clothingCustomizerItem = itemInfo;
    showClothingCustomizer = true;
  }

  function handleClothingConfirm(result) {
    if (result && clothingCustomizerItem) {
      const bucket = "clothing";
      const groupName = clothingCustomizerItem.group;
      if (!selections[bucket]) selections[bucket] = {};
      selections[bucket][groupName] = {
        tag: result.tag || clothingCustomizerItem.tag,
        tags: result.tags,
        natlang: result.natlang,
        display: result.display,
        isCustomized: result.isCustomized,
      };
      selections = { ...selections };
    }
    showClothingCustomizer = false;
    clothingCustomizerItem = null;
  }

  function handleClothingCancel() {
    showClothingCustomizer = false;
    clothingCustomizerItem = null;
  }

  // --- Fantasy Customizer ---
  function handleOpenFantasyCustomizer(itemInfo) {
    fantasyCustomizerItem = itemInfo;
    showFantasyCustomizer = true;
  }

  function handleFantasyConfirm(result) {
    if (result && fantasyCustomizerItem) {
      const bucket = "appearance";
      const groupName = "fantasy";
      if (!selections[bucket]) selections[bucket] = {};
      selections[bucket][groupName] = {
        tag: result.tag || fantasyCustomizerItem.tag,
        tags: result.tags,
        natlang: result.natlang,
        display: result.display,
        isCustomized: result.isCustomized,
      };
      selections = { ...selections };
    }
    showFantasyCustomizer = false;
    fantasyCustomizerItem = null;
  }

  function handleFantasyCancel() {
    showFantasyCustomizer = false;
    fantasyCustomizerItem = null;
  }

  // --- Selection preview remove handler ---
  function handleEditBubble(removeInfo) {
    if (removeInfo.bucket !== "characters") return;
    const char = selections.characters?.[removeInfo.index];
    if (!char) return;
    characterModalTag = char.tag;
    characterModalDisplay = char.display || char.tag;
    showCharacterModal = true;
  }

  function handleRemove(removeInfo) {
    const { bucket, index, type, groupName, tag } = removeInfo;

    if (bucket === "characters") {
      const char = selections.characters[index];
      if (char) {
        if (type === "character") char.base = false;
        else if (type === "outfit") { char.outfit = null; char.outfitIndex = null; }
        else if (type === "pose") { char.pose = null; char.poseIndex = null; }

        // If nothing's left for this character, drop it entirely.
        if (!char.base && !char.outfit && !char.pose) {
          selections.characters.splice(index, 1);
        }
      }
    } else if (bucket === "props") {
      selections.props.splice(index, 1);
    } else if (type === "mixer") {
      const sel = selections[bucket]?.[groupName];
      if (Array.isArray(sel)) {
        const idx = sel.findIndex(s => s.tag === tag);
        if (idx >= 0) sel.splice(idx, 1);
        if (sel.length === 0) delete selections[bucket][groupName];
      } else {
        delete selections[bucket][groupName];
      }
    }

    selections = { ...selections };
  }

  // --- Insert handler ---
  function handleInsert() {
    const stateSnapshot = {
      isNaturalMode,
      selections,
      cache,
    };
    const text = buildInsertText(stateSnapshot, true, tagSourceConfig);
    onInsert(text);
  }

  // --- Dropdown positioning action (for All tab's fixed-position dropdowns) ---
  function positionDropdown(dropdownNode) {
    function reposition() {
      const groupEl = dropdownNode.closest(".pcr-atb-mixer-group");
      const header = groupEl?.querySelector(".pcr-atb-mixer-header");
      if (!header) return;

      const headerRect = header.getBoundingClientRect();
      const isCharCategory = groupEl.classList.contains("pcr-atb-char-category");
      const maxH = isCharCategory ? 350 : 200;
      const dropH = Math.min(dropdownNode.scrollHeight, maxH);
      const vh = window.innerHeight;
      const spaceBelow = vh - headerRect.bottom;
      const spaceAbove = headerRect.top;

      if (spaceBelow < dropH && spaceAbove > spaceBelow) {
        groupEl.classList.add("flip-up");
        dropdownNode.style.left = `${headerRect.left}px`;
        dropdownNode.style.width = `${headerRect.width}px`;
        dropdownNode.style.bottom = `${vh - headerRect.top}px`;
        dropdownNode.style.top = "auto";
        dropdownNode.style.maxHeight = `${Math.min(spaceAbove - 10, maxH)}px`;
      } else {
        groupEl.classList.remove("flip-up");
        dropdownNode.style.left = `${headerRect.left}px`;
        dropdownNode.style.width = `${headerRect.width}px`;
        dropdownNode.style.top = `${headerRect.bottom}px`;
        dropdownNode.style.bottom = "auto";
        dropdownNode.style.maxHeight = `${Math.min(spaceBelow - 10, maxH)}px`;
      }
    }

    reposition();
    // Dropdowns use position:fixed, so we have to manually re-anchor when the
    // user scrolls the content area or resizes the window — otherwise the
    // dropdown stays put while its trigger moves under it.
    const scrollContainer = dropdownNode.closest(".pcr-atb-content");
    if (scrollContainer) scrollContainer.addEventListener("scroll", reposition, { passive: true });
    window.addEventListener("resize", reposition);
    return {
      update: reposition,
      destroy() {
        if (scrollContainer) scrollContainer.removeEventListener("scroll", reposition);
        window.removeEventListener("resize", reposition);
      },
    };
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="pcr-atb-panel">
  <div class="pcr-atb-header">
    <span class="pcr-atb-title">Tag Builder</span>
    <div class="pcr-atb-search-wrapper">
      <input
        class="pcr-atb-search"
        placeholder="Search..."
        bind:value={searchQuery}
        bind:this={searchInputEl}
        oninput={handleSearchInput}
      >
      <button
        class="pcr-atb-search-clear"
        class:hidden={!searchQuery}
        title="Clear search"
        onclick={clearSearch}
      >&times;</button>
    </div>
  </div>

  <div class="pcr-atb-body">
    <div class="pcr-atb-tabs">
      {#each TABS as tab}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div
          class="pcr-atb-tab"
          class:active={activeTab === tab.key}
          onclick={() => switchTab(tab.key)}
        >
          <span class="pcr-atb-tab-icon">{tab.icon}</span>
          <span class="pcr-atb-tab-label">{tab.label}</span>
        </div>
      {/each}
    </div>

    <div class="pcr-atb-content">
      {#if loading}
        <div class="pcr-atb-loading-overlay">
          <div class="pcr-atb-loading-spinner"></div>
          <div class="pcr-atb-loading-text">Loading {TABS.find(t => t.key === activeTab)?.label || activeTab}...</div>
        </div>

      {:else if activeTab === "all"}
        <!-- All tab: shows all buckets as sections with mixer grids -->
        <div class="pcr-atb-all-mixer-wrapper">
          {#each ALL_TAB_BUCKETS as bucket}
            {@const groups = getAllFilteredGroups(bucket)}
            {#if groups.length > 0}
              {@const info = BUCKET_INFO[bucket] || { label: bucket, icon: "\uD83D\uDCC1" }}
              <div class="pcr-atb-all-section" data-bucket={bucket}>
                <div class="pcr-atb-all-section-header">
                  <span class="icon">{info.icon}</span>{info.label}
                </div>
                {#if bucket === "characters"}
                  <!-- Character groups use series-grouped dropdowns -->
                  <div class="pcr-atb-mixer-grid">
                    {#each groups as group}
                      {@const charsInCategory = (selections.characters || []).filter(c => group.items.some(i => i.tag === c.tag))}
                      {@const selectedTags = new Set(charsInCategory.map(c => c.tag))}
                      {@const displayVal = charsInCategory.length === 0 ? "Select" : charsInCategory.length === 1 ? charsInCategory[0].display : `${charsInCategory.length} selected`}
                      <!-- svelte-ignore a11y_click_events_have_key_events -->
                      {@const isCharOpen = openDropdownKey === `char:${group.category}`}
                      {@const charFlatList = isCharOpen ? (group.seriesData || []).flatMap(s => filterDropdownItems(s.characters.filter(c => group.items.some(i => i.tag === c.tag)), c => c.display || c.tag, c => c.tag)) : []}
                      <div class="pcr-atb-mixer-group pcr-atb-char-category" class:open={isCharOpen} data-group={group.name}>
                        <div class="pcr-atb-mixer-header" onclick={isCharOpen ? null : (e) => { e.stopPropagation(); toggleCharCategory(group.category); }}>
                          {#if isCharOpen}
                            <div class="pcr-atb-mixer-search-wrap">
                              <input
                                class="pcr-atb-mixer-search"
                                type="text"
                                placeholder="Search {group.display}…"
                                value={dropdownSearchQuery}
                                oninput={(e) => { dropdownSearchQuery = e.target.value; }}
                                onclick={(e) => e.stopPropagation()}
                                onkeydown={(e) => handleDropdownKey(e, charFlatList, (char) => { handleAllCharacterClick(char.tag, char.display || char.tag); openDropdownKey = null; })}
                                use:focusOnMount
                              />
                              <button type="button" class="pcr-atb-mixer-close" aria-label="Close" onclick={(e) => { e.stopPropagation(); openDropdownKey = null; }}>&times;</button>
                            </div>
                          {:else}
                            <span class="pcr-atb-mixer-label">{group.display} ({group.items.length})</span>
                            <span class="pcr-atb-mixer-value" class:has-value={charsInCategory.length > 0}>{displayVal}</span>
                          {/if}
                        </div>
                        {#if isCharOpen}
                          <div class="pcr-atb-mixer-dropdown pcr-atb-char-dropdown" use:positionDropdown>
                            {#each group.seriesData || [] as series}
                              {@const seriesCharsInGroup = series.characters.filter(c => group.items.some(i => i.tag === c.tag))}
                              {@const seriesChars = filterDropdownItems(seriesCharsInGroup, c => c.display || c.tag, c => c.tag)}
                              {#if seriesChars.length > 0}
                                <div class="pcr-atb-char-series-group">
                                  <div class="pcr-atb-char-series-header">{series.name}</div>
                                  {#each seriesChars as char}
                                    {@const flatIdx = charFlatList.indexOf(char)}
                                    {@const isHighlighted = flatIdx === highlightedIndex}
                                    <div
                                      class="pcr-atb-mixer-option pcr-atb-char-option"
                                      class:selected={selectedTags.has(char.tag)}
                                      class:highlighted={isHighlighted}
                                      onclick={(e) => { e.stopPropagation(); handleAllCharacterClick(char.tag, char.display || char.tag); openDropdownKey = null; }}
                                      use:highlightScroll={isHighlighted}
                                    >
                                      <span class="pcr-atb-char-option-name">{char.display || char.tag}</span>
                                    </div>
                                  {/each}
                                </div>
                              {/if}
                            {/each}
                          </div>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {:else if bucket === "props"}
                  <!-- Props groups as mixer dropdowns -->
                  <div class="pcr-atb-mixer-grid">
                    {#each groups as group}
                      {@const propsInCategory = (selections.props || []).filter(p => p.category === group.category)}
                      {@const selectedTagsSet = new Set(propsInCategory.map(p => p.prop))}
                      {@const propDisplayVal = propsInCategory.length === 0 ? "Select" : propsInCategory.length === 1 ? propsInCategory[0].display : `${propsInCategory.length} selected`}
                      <!-- svelte-ignore a11y_click_events_have_key_events -->
                      {@const isPropsOpen = openDropdownKey === `props:${group.name}`}
                      {@const visiblePropItems = isPropsOpen ? filterDropdownItems(group.items, i => i.display || i.tag, i => i.tag) : []}
                      <div class="pcr-atb-mixer-group" class:open={isPropsOpen} data-group={group.name}>
                        <div class="pcr-atb-mixer-header" onclick={isPropsOpen ? null : (e) => { e.stopPropagation(); setOpenDropdown(`props:${group.name}`); }}>
                          {#if isPropsOpen}
                            <div class="pcr-atb-mixer-search-wrap">
                              <input
                                class="pcr-atb-mixer-search"
                                type="text"
                                placeholder="Search {group.display}…"
                                value={dropdownSearchQuery}
                                oninput={(e) => { dropdownSearchQuery = e.target.value; }}
                                onclick={(e) => e.stopPropagation()}
                                onkeydown={(e) => handleDropdownKey(e, visiblePropItems, (item) => { openDropdownKey = null; handleAllPropsClick(item.category || group.category, item.tag); })}
                                use:focusOnMount
                              />
                              <button type="button" class="pcr-atb-mixer-close" aria-label="Close" onclick={(e) => { e.stopPropagation(); openDropdownKey = null; }}>&times;</button>
                            </div>
                          {:else}
                            <span class="pcr-atb-mixer-label">{group.display} ({group.items.length})</span>
                            <span class="pcr-atb-mixer-value" class:has-value={propsInCategory.length > 0}>{propDisplayVal}</span>
                          {/if}
                        </div>
                        {#if isPropsOpen}
                          <div class="pcr-atb-mixer-dropdown" use:positionDropdown>
                            <div class="pcr-atb-mixer-option pcr-atb-mixer-none" onclick={(e) => { e.stopPropagation(); selections.props = (selections.props || []).filter(p => p.category !== group.category); selections = {...selections}; openDropdownKey = null; }}>
                              -- None --
                            </div>
                            {#each visiblePropItems as item, idx}
                              {@const isHighlighted = idx === highlightedIndex}
                              <div
                                class="pcr-atb-mixer-option"
                                class:selected={selectedTagsSet.has(item.tag)}
                                class:highlighted={isHighlighted}
                                onclick={(e) => { e.stopPropagation(); openDropdownKey = null; handleAllPropsClick(item.category || group.category, item.tag); }}
                                use:highlightScroll={isHighlighted}
                              >
                                {item.display || item.tag}{item.is_customizable ? " \u2605" : ""}
                              </div>
                            {/each}
                            {#if visiblePropItems.length === 0}
                              <div class="pcr-atb-mixer-empty">No matches</div>
                            {/if}
                          </div>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {:else if bucket === "clothing"}
                  <ClothingPanel
                    groups={groups}
                    selections={selections.clothing || {}}
                    {isNaturalMode}
                    {searchQuery}
                    onSelect={handleMixerSelect}
                    onOpenClothingCustomizer={handleOpenClothingCustomizer}
                  />
                {:else}
                  <!-- Standard mixer buckets -->
                  <MixerGrid
                    groups={groups}
                    {bucket}
                    selections={selections[bucket] || {}}
                    {isNaturalMode}
                    {searchQuery}
                    openGroup={mixerOpenGroupFor(bucket)}
                    onSetOpenGroup={(g) => setMixerOpenGroup(bucket, g)}
                    {dropdownSearchQuery}
                    onSetDropdownSearchQuery={(q) => { dropdownSearchQuery = q; }}
                    {highlightedIndex}
                    onSetHighlightedIndex={(i) => { highlightedIndex = i; }}
                    onSelect={handleAllSelect}
                    onOpenNsfwModal={bucket === "action" ? handleOpenNsfwModal : null}
                    onOpenFantasyCustomizer={bucket === "appearance" ? handleOpenFantasyCustomizer : null}
                  />
                {/if}
              </div>
            {/if}
          {/each}
        </div>

      {:else if activeTab === "characters"}
        <!-- Characters tab: category dropdowns -->
        {#if characterCategories.length === 0}
          <div class="pcr-atb-empty">
            {searchQuery ? "No characters found" : "No characters in database"}
          </div>
        {:else}
          <div class="pcr-atb-all-mixer-wrapper">
          <div class="pcr-atb-mixer-grid">
            {#each characterCategories as category}
              {@const charCount = category.series.reduce((sum, s) => sum + s.characters.length, 0)}
              {@const selectedChars = (selections.characters || []).filter(c => category.series.some(s => s.characters.some(ch => ch.tag === c.tag)))}
              {@const selectedCharTags = new Set(selectedChars.map(c => c.tag))}
              {@const charDisplayVal = selectedChars.length === 0 ? "Select" : selectedChars.length === 1 ? selectedChars[0].display : `${selectedChars.length} selected`}
              <!-- svelte-ignore a11y_click_events_have_key_events -->
              {@const isCatOpen = openDropdownKey === `char:${category.tag}`}
              {@const catFlatList = isCatOpen ? category.series.flatMap(s => filterDropdownItems(s.characters, c => c.display || c.tag, c => c.tag)) : []}
              <div class="pcr-atb-mixer-group pcr-atb-char-category" class:open={isCatOpen} data-category={category.tag}>
                <div class="pcr-atb-mixer-header" onclick={isCatOpen ? null : (e) => { e.stopPropagation(); toggleCharCategory(category.tag); }}>
                  {#if isCatOpen}
                    <div class="pcr-atb-mixer-search-wrap">
                      <input
                        class="pcr-atb-mixer-search"
                        type="text"
                        placeholder="Search {category.name}…"
                        value={dropdownSearchQuery}
                        oninput={(e) => { dropdownSearchQuery = e.target.value; }}
                        onclick={(e) => e.stopPropagation()}
                        onkeydown={(e) => handleDropdownKey(e, catFlatList, (char) => handleCharOptionClick(char.tag, char.display || char.tag))}
                        use:focusOnMount
                      />
                      <button type="button" class="pcr-atb-mixer-close" aria-label="Close" onclick={(e) => { e.stopPropagation(); openDropdownKey = null; }}>&times;</button>
                    </div>
                  {:else}
                    <span class="pcr-atb-mixer-label">{category.name} ({charCount})</span>
                    <span class="pcr-atb-mixer-value" class:has-value={selectedChars.length > 0}>{charDisplayVal}</span>
                  {/if}
                </div>
                {#if isCatOpen}
                  <div class="pcr-atb-mixer-dropdown pcr-atb-char-dropdown" use:positionDropdown>
                    {#each category.series as series}
                      {@const visibleChars = filterDropdownItems(series.characters, c => c.display || c.tag, c => c.tag)}
                      {#if visibleChars.length > 0}
                        <div class="pcr-atb-char-series-group">
                          <div class="pcr-atb-char-series-header">{series.name}</div>
                          {#each visibleChars as char}
                            {@const flatIdx = catFlatList.indexOf(char)}
                            {@const isHighlighted = flatIdx === highlightedIndex}
                            <div
                              class="pcr-atb-mixer-option pcr-atb-char-option"
                              class:selected={selectedCharTags.has(char.tag)}
                              class:highlighted={isHighlighted}
                              onclick={(e) => { e.stopPropagation(); handleCharOptionClick(char.tag, char.display || char.tag); }}
                              use:highlightScroll={isHighlighted}
                            >
                              <span class="pcr-atb-char-option-name">{char.display || char.tag}</span>
                            </div>
                          {/each}
                        </div>
                      {/if}
                    {/each}
                  </div>
                {/if}
              </div>
            {/each}
          </div>
          </div>
        {/if}

      {:else if activeTab === "props"}
        <!-- Props tab: category buttons or search grid -->
        {#if searchQuery && propsSearchGroups.length > 0}
          <!-- Search results as mixer dropdowns per category -->
          <div class="pcr-atb-props">
            <div class="pcr-atb-all-mixer-wrapper">
              {#each propsSearchGroups as { category: cat, props: filteredProps }}
                {@const propsInCategory = (selections.props || []).filter(p => p.category === cat.category)}
                {@const selectedPropTags = new Set(propsInCategory.map(p => p.prop))}
                {@const propDisplayVal = propsInCategory.length === 0 ? "Select" : propsInCategory.length === 1 ? propsInCategory[0].display : `${propsInCategory.length} selected`}
                {@const isSearchPropOpen = openDropdownKey === `props:${cat.category}`}
                {@const visibleFilteredProps = isSearchPropOpen ? filterDropdownItems(filteredProps, p => p.display_name || p.prop_tag, p => p.prop_tag) : []}
                <div class="pcr-atb-all-section" data-bucket="props">
                  <div class="pcr-atb-all-section-header">
                    <span class="icon">{cat.icon}</span>{cat.display_name}
                  </div>
                  <div class="pcr-atb-mixer-grid">
                    <!-- svelte-ignore a11y_click_events_have_key_events -->
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <div class="pcr-atb-mixer-group" class:open={isSearchPropOpen} data-group="_props_{cat.category}">
                      <div class="pcr-atb-mixer-header" onclick={isSearchPropOpen ? null : (e) => { e.stopPropagation(); setOpenDropdown(`props:${cat.category}`); }}>
                        {#if isSearchPropOpen}
                          <div class="pcr-atb-mixer-search-wrap">
                            <input
                              class="pcr-atb-mixer-search"
                              type="text"
                              placeholder="Search {cat.display_name}…"
                              value={dropdownSearchQuery}
                              oninput={(e) => { dropdownSearchQuery = e.target.value; }}
                              onclick={(e) => e.stopPropagation()}
                              onkeydown={(e) => handleDropdownKey(e, visibleFilteredProps, (p) => { openDropdownKey = null; handleAllPropsClick(cat.category, p.prop_tag); })}
                              use:focusOnMount
                            />
                            <button type="button" class="pcr-atb-mixer-close" aria-label="Close" onclick={(e) => { e.stopPropagation(); openDropdownKey = null; }}>&times;</button>
                          </div>
                        {:else}
                          <span class="pcr-atb-mixer-label">{cat.icon} {cat.display_name}</span>
                          <span class="pcr-atb-mixer-value" class:has-value={propsInCategory.length > 0}>{propDisplayVal}</span>
                        {/if}
                      </div>
                      {#if isSearchPropOpen}
                        <div class="pcr-atb-mixer-dropdown" use:positionDropdown>
                          <div class="pcr-atb-mixer-option pcr-atb-mixer-none" onclick={(e) => { e.stopPropagation(); selections.props = (selections.props || []).filter(p => p.category !== cat.category); selections = {...selections}; openDropdownKey = null; }}>
                            -- None --
                          </div>
                          {#each visibleFilteredProps as p, idx}
                            {@const isHighlighted = idx === highlightedIndex}
                            <div
                              class="pcr-atb-mixer-option"
                              class:selected={selectedPropTags.has(p.prop_tag)}
                              class:highlighted={isHighlighted}
                              onclick={(e) => { e.stopPropagation(); openDropdownKey = null; handleAllPropsClick(cat.category, p.prop_tag); }}
                              use:highlightScroll={isHighlighted}
                            >
                              {p.display_name || p.prop_tag}{p.is_customizable ? " \u2605" : ""}
                            </div>
                          {/each}
                          {#if visibleFilteredProps.length === 0}
                            <div class="pcr-atb-mixer-empty">No matches</div>
                          {/if}
                        </div>
                      {/if}
                    </div>
                  </div>
                </div>
              {/each}
            </div>

            {#if (selections.props || []).length > 0}
              <div class="pcr-atb-props-selections">
                <div class="pcr-atb-props-selections-label">Selected Props:</div>
                <div class="pcr-atb-props-selections-pills">
                  {#each selections.props as prop, idx}
                    <!-- svelte-ignore a11y_click_events_have_key_events -->
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <div class="pcr-atb-props-pill">
                      <span class="pill-text">{prop.display}</span>
                      <span class="pill-remove" onclick={() => removeProp(idx)}>&times;</span>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {:else if searchQuery}
          <div class="pcr-atb-empty">No props found matching "{searchQuery}"</div>
        {:else}
          <!-- Category buttons -->
          <div class="pcr-atb-props">
            <div class="pcr-atb-props-buttons">
              {#each cache.propsData?.categories || [] as cat}
                <button
                  class="pcr-atb-props-btn"
                  onclick={() => handlePropsCategoryClick(cat.category)}
                >
                  {cat.icon} {cat.display_name}
                </button>
              {/each}
            </div>

            {#if (selections.props || []).length > 0}
              <div class="pcr-atb-props-selections">
                <div class="pcr-atb-props-selections-label">Selected Props:</div>
                <div class="pcr-atb-props-selections-pills">
                  {#each selections.props as prop, idx}
                    <!-- svelte-ignore a11y_click_events_have_key_events -->
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <div class="pcr-atb-props-pill">
                      <span class="pill-text">{prop.display}</span>
                      <span class="pill-remove" onclick={() => removeProp(idx)}>&times;</span>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/if}

      {:else if activeTab === "clothing"}
        <ClothingPanel
          groups={tabData[activeTab] || cache[activeTab] || []}
          selections={selections.clothing || {}}
          {isNaturalMode}
          {searchQuery}
          onSelect={handleMixerSelect}
          onOpenClothingCustomizer={handleOpenClothingCustomizer}
        />

      {:else}
        <!-- Generic mixer tab (cast, appearance, pose, scene, expression, action) -->
        <MixerGrid
          groups={tabData[activeTab] || cache[activeTab] || []}
          bucket={activeTab}
          selections={selections[activeTab] || {}}
          {isNaturalMode}
          {searchQuery}
          openGroup={mixerOpenGroupFor(activeTab)}
          onSetOpenGroup={(g) => setMixerOpenGroup(activeTab, g)}
          {dropdownSearchQuery}
          onSetDropdownSearchQuery={(q) => { dropdownSearchQuery = q; }}
          {highlightedIndex}
          onSetHighlightedIndex={(i) => { highlightedIndex = i; }}
          onSelect={handleMixerSelect}
          onOpenNsfwModal={activeTab === "action" ? handleOpenNsfwModal : null}
          onOpenFantasyCustomizer={activeTab === "appearance" ? handleOpenFantasyCustomizer : null}
        />
      {/if}
    </div>
  </div>

  <SelectionPreview {selections} {cache} onRemove={handleRemove} onEdit={handleEditBubble} />

  <div class="pcr-atb-footer">
    <div class="pcr-atb-toggle">
      <label class="pcr-atb-toggle-option">
        <input type="radio" name="pcr-atb-format" value="tags" checked={!isNaturalMode} onchange={() => { isNaturalMode = false; onPromptStyleChange("tags"); }}>
        <span>Tags</span>
      </label>
      <label class="pcr-atb-toggle-option">
        <input type="radio" name="pcr-atb-format" value="natural" checked={isNaturalMode} onchange={() => { isNaturalMode = true; onPromptStyleChange("natural"); }}>
        <span>Natural Language</span>
      </label>
    </div>
    <div class="pcr-atb-buttons">
      <button class="pcr-atb-cancel" onclick={onClose}>Cancel</button>
      <button class="pcr-atb-insert" onclick={handleInsert}>Insert</button>
    </div>
  </div>
</div>

<!-- Modals -->
{#if showCharacterModal}
  <CharacterModal
    characterTag={characterModalTag}
    characterDisplay={characterModalDisplay}
    existing={(selections.characters || []).find(c => c.tag === characterModalTag) || null}
    {cache}
    onConfirm={handleCharacterSelected}
    onCancel={() => { showCharacterModal = false; }}
  />
{/if}

{#if showPropsModal && cache.propsData}
  <PropsCustomizerModal
    category={propsModalCategory}
    propsData={cache.propsData}
    preSelectedProp={propsModalPreSelected}
    {isNaturalMode}
    onConfirm={handlePropConfirm}
    onCancel={() => { showPropsModal = false; }}
  />
{/if}

{#if showNsfwModal}
  <NsfwActionModal
    groups={nsfwModalGroups}
    currentSelections={selections.nsfw_action || {}}
    onConfirm={handleNsfwConfirm}
    onCancel={() => { showNsfwModal = false; }}
  />
{/if}

{#if showClothingCustomizer && clothingCustomizerItem}
  <ClothingCustomizer
    itemInfo={clothingCustomizerItem}
    onConfirm={handleClothingConfirm}
    onCancel={handleClothingCancel}
  />
{/if}

{#if showFantasyCustomizer && fantasyCustomizerItem}
  <FantasyCustomizer
    itemInfo={fantasyCustomizerItem}
    onConfirm={handleFantasyConfirm}
    onCancel={handleFantasyCancel}
  />
{/if}
