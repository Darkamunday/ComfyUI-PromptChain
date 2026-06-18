"""Fact-delta parser + applier for the natlang render pipeline.

Phase B + C of the vibrant-rendering-loom plan.

Replaces the v1 "structural intent + state mutation in apply_intent_to_state"
flow with a two-stage path:

  1. parse_user_request_to_deltas(user_request, bios, current_state)
        → ordered list[Delta]    (Phase B)

  2. apply_deltas(state, deltas, bios)
        → mutated PromptState    (Phase C)

The order in step 1 is:
  SwapCharacter → SwapOutfit → Strip → FillSlot → ApplyModifier
                → PoseChange / SetExpression / SetSetting / SwapStyle

Sequential application means each delta sees the result of the previous one.
No render between deltas — render runs ONCE on the final cumulative state.

Modifier propagation is folded into apply_deltas (no separate
`apply_reverse_displacement` pass): after every slot fill,
update_active_modifiers_from_slots drops modifiers whose clears are all
filled now. This is the "fact propagation" pattern from the plan.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .prompt_state import (
    PromptState, OutfitState, PoseState, CharacterState, SlotState, StyleState,
    SLOT_NAMES, SLOT_STATE_FILLED, SLOT_STATE_CLEARED,
    ORIGIN_BIO, ORIGIN_USER, ORIGIN_SWAP, ORIGIN_NONE,
    _empty_slot_states,
)


class DecomposeUnavailable(Exception):
    """Raised when LLM decompose can't produce useful sub-intents.
    Caller should surface this as a hard failure — the v2 architecture
    requires the LLM to function; we do not run a regex fallback."""


# ── delta types ─────────────────────────────────────────────────────

@dataclass
class SwapCharacterDelta:
    """Replace the entire character block. Cammy's bio + default outfit
    + matched pose are wiped; new character's bio takes over."""
    target_tag: str
    target_display: str = ""


@dataclass
class SwapOutfitDelta:
    """Replace outfit identity. slot_states are reloaded from the bio
    outfit's slot rows. active_modifiers persist (compatible mods stay).
    user_extra_phrases persist.

    source_character_display names the source character when the outfit
    was borrowed from another bio (e.g. `cammy white in chun-li's outfit`
    → source='Chun-Li'). Empty when the outfit belongs to the subject."""
    outfit_name: str
    outfit_id: Optional[int] = None
    bio_slots: list[dict] = field(default_factory=list)
    bio_natlang: str = ""
    source_character_display: str = ""


@dataclass
class StripDelta:
    """`wearing only X` — kept_slots survive, all others become cleared
    with by_modifier="strip". User-filled slots in kept_slots survive."""
    kept_slots: list[str] = field(default_factory=list)


@dataclass
class FillSlotDelta:
    """`wearing red socks` — fills a single slot. If origin=user and the
    bio outfit had nothing in that slot, render synthesizes a brief
    sentence anchor."""
    slot: str
    item: str
    color: str = ""
    origin: str = ORIGIN_USER


@dataclass
class ApplyModifierDelta:
    """`barefoot` — adds canonical to active_modifiers, clears each slot
    in clears_slots (unless that slot already has an explicit user fill)."""
    canonical: str
    clears_slots: list[str] = field(default_factory=list)
    substitute_section: str = ""  # "outfit" | "pose" | ""


@dataclass
class SwapPoseDelta:
    """Replace pose identity using bio data — typically a cross-character
    borrow like `tifa lockhart in cammy white's victory pose`. Mirrors
    SwapOutfitDelta: bio data overwrites the primary character's pose
    state, so subsequent renders use the borrowed bio's anchor verbatim.

    Set is_signature when the borrowed pose is the source character's
    signature pose — propagates to the rendered header label.

    source_character_display names the source bio when the pose was
    borrowed from another character. Drives the rendered header label
    so readers see the pose's true origin."""
    pose_name: str
    pose_id: Optional[int] = None
    bio_natlang: str = ""
    is_signature: bool = False
    source_character_display: str = ""


@dataclass
class ClearSlotDelta:
    """User explicitly removed an item from a specific slot via "no X" /
    "remove X" / "without X" / "drop the X" / "take off the X" etc.
    Per-slot clearing — does not invoke any modifier or its associated
    clears_slots. Use slot_modifier-driven clears (ApplyModifierDelta)
    when the intent maps to a known modifier; use ClearSlotDelta for
    direct slot-specific removals.

    Apply: set slot to CLEARED with by_modifier='user_remove',
    preserving prior item/color for downstream body-prose stripping."""
    slot: str


@dataclass
class ApplyPoseChipDelta:
    """Generic pose chip from the curated pose_items pool (`presenting_feet`,
    `top-down_bottom-up`, etc.). Mirrors SwapPoseDelta's render side but
    bio_pose_id stays None so the header omits character attribution —
    the chip is character-agnostic. Resolved by bge-small semantic search
    over pose_items.base_natlang via bucket_search."""
    chip_tag: str
    display_name: str = ""
    base_natlang: str = ""
    base_tags: str = ""


@dataclass
class PoseChangeDelta:
    """`standing up` — replaces drops the named facts (silent if absent),
    adds appends new ones. replaces_all wipes descriptive_facts entirely
    before adding (fresh-pose semantics — any turn that mentions pose
    starts a new descriptive_facts list, so carry-over from earlier
    turns doesn't accumulate). Posture verbs hit this delta."""
    replaces: list[str] = field(default_factory=list)
    adds: list[str] = field(default_factory=list)
    is_anchor_override: bool = False  # True when phrase carries a posture verb
    replaces_all: bool = False


@dataclass
class SetExpressionDelta:
    text: str = ""


@dataclass
class SetSettingDelta:
    text: str = ""


@dataclass
class SwapStyleDelta:
    template_id: str = ""
    name: str = ""


@dataclass
class ClearSettingDelta:
    """`remove scene` / `no setting` / `clear the background` — wipe
    state.setting so the // Scene section is omitted from output."""


@dataclass
class ClearExpressionDelta:
    """`remove expression` / `no expression` / `clear the face` — wipe
    state.expression. For an explicit neutral, use SetExpression('neutral')."""


@dataclass
class ClearPoseDelta:
    """`remove pose` / `no pose` / `clear the pose` — fully reset pose
    state (bio anchor, descriptive_facts, modifiers, source attribution)."""


@dataclass
class ClearStyleDelta:
    """`remove style` / `no style` — wipe state.style entirely. Rare but
    supported for symmetry; most prompts benefit from some style guidance."""


Delta = (
    SwapCharacterDelta | SwapOutfitDelta | StripDelta | FillSlotDelta
    | ClearSlotDelta | ApplyModifierDelta
    | SwapPoseDelta | ApplyPoseChipDelta | PoseChangeDelta
    | SetExpressionDelta | SetSettingDelta | SwapStyleDelta
    | ClearSettingDelta | ClearExpressionDelta | ClearPoseDelta | ClearStyleDelta
)


# Application order: index in this tuple determines sort priority. Earlier
# deltas run first. Within the same kind, deltas keep parser order.
# SwapPoseDelta runs before ApplyPoseChipDelta before PoseChangeDelta —
# a cross-character bio borrow wins over a generic chip match, both win
# over freeform user phrases.
_DELTA_ORDER = (
    SwapCharacterDelta,
    SwapOutfitDelta,
    StripDelta,
    FillSlotDelta,
    ClearSlotDelta,
    ApplyModifierDelta,
    SwapPoseDelta,
    ApplyPoseChipDelta,
    PoseChangeDelta,
    ClearPoseDelta,
    SetExpressionDelta,
    ClearExpressionDelta,
    SetSettingDelta,
    ClearSettingDelta,
    SwapStyleDelta,
    ClearStyleDelta,
)


def _delta_priority(d: Delta) -> int:
    for i, cls in enumerate(_DELTA_ORDER):
        if isinstance(d, cls):
            return i
    return len(_DELTA_ORDER)


# ── slot resolution from prose ──────────────────────────────────────

# Maps a clothing keyword to its canonical slot. Used by FillSlotDelta
# resolution when the user types free-form like "wearing red socks" and
# we need to know that "socks" goes to legwear.
_SLOT_KEYWORDS: dict[str, str] = {
    # tops
    "leotard": "tops", "shirt": "tops", "blouse": "tops", "jacket": "tops",
    "vest": "tops", "bra": "tops", "bodysuit": "tops", "unitard": "tops",
    "sweater": "tops", "t-shirt": "tops", "tshirt": "tops", "tank": "tops",
    "halter": "tops", "crop_top": "tops", "kimono_top": "tops", "tunic": "tops",
    # bottoms
    "pants": "bottoms", "shorts": "bottoms", "panties": "bottoms",
    "skirt": "bottoms", "miniskirt": "bottoms", "jeans": "bottoms",
    "hot_pants": "bottoms", "hakama": "bottoms", "kimono_bottom": "bottoms",
    "trousers": "bottoms",
    # dresses
    "dress": "dresses", "gown": "dresses", "kimono": "dresses", "qipao": "dresses",
    "sundress": "dresses", "china_dress": "dresses",
    # headwear
    "hat": "headwear", "cap": "headwear", "hood": "headwear", "mask": "headwear",
    "beret": "headwear", "helmet": "headwear", "headband": "headwear",
    "headphones": "headwear", "garrison_cap": "headwear",
    # footwear
    "boots": "footwear", "shoes": "footwear", "sneakers": "footwear",
    "heels": "footwear", "sandals": "footwear", "loafers": "footwear",
    "skates": "footwear", "flip_flops": "footwear", "flipflops": "footwear",
    # legwear
    "pantyhose": "legwear", "socks": "legwear", "thighhighs": "legwear",
    "stockings": "legwear", "leggings": "legwear", "kneehighs": "legwear",
    # handwear
    "gloves": "handwear", "gauntlets": "handwear", "mittens": "handwear",
    "wraps": "handwear", "bracers": "handwear",
    # lingerie
    "garter_belt": "lingerie", "corset": "lingerie", "teddy": "lingerie",
    # swimwear
    "bikini": "swimwear", "swimsuit": "swimwear", "school_swimsuit": "swimwear",
    "one-piece_swimsuit": "swimwear",
    # neckwear
    "necktie": "neckwear", "tie": "neckwear", "scarf": "neckwear",
    "choker": "neckwear", "bowtie": "neckwear", "cravat": "neckwear",
    # accessories
    "bracelet": "accessories", "earrings": "accessories", "ring": "accessories",
    "necklace": "accessories", "badge": "accessories", "holster": "accessories",
    "harness": "accessories", "knee_pads": "accessories", "elbow_pads": "accessories",
    "dog_tags": "accessories", "sunglasses": "accessories", "glasses": "accessories",
    "veil": "accessories", "bouquet": "accessories", "ribbon": "accessories",
}


def _resolve_slot_from_phrase(phrase: str) -> Optional[str]:
    """Find the canonical slot for a free-form clothing phrase. Returns
    e.g. "legwear" for "red socks" or "tops" for "green leotard".
    Token order doesn't matter — last keyword wins (typical phrase has
    color first then item, so this finds the item)."""
    normalized = re.sub(r"[^\w\s]", " ", phrase.lower())
    tokens = [t for t in re.split(r"\s+", normalized) if t]
    found_slot = None
    for tok in tokens:
        if tok in _SLOT_KEYWORDS:
            found_slot = _SLOT_KEYWORDS[tok]
        # multi-word keys: try pairs
    for i in range(len(tokens) - 1):
        pair = f"{tokens[i]}_{tokens[i+1]}"
        if pair in _SLOT_KEYWORDS:
            found_slot = _SLOT_KEYWORDS[pair]
    return found_slot


_KNOWN_COLORS: frozenset[str] = frozenset({
    "red", "blue", "green", "yellow", "black", "white", "purple",
    "pink", "orange", "brown", "grey", "gray", "navy", "crimson",
    "scarlet", "violet", "magenta", "cyan", "teal", "beige", "tan",
    "gold", "silver", "bronze", "copper", "turquoise", "olive",
    "kelly", "light", "dark",
})


def _split_color_item(phrase: str) -> tuple[str, str]:
    """Split a phrase into (color, item) when phrase is `<color>? <item>`.
    Used as a fallback when slot_for-aware extraction isn't appropriate."""
    tokens = re.findall(r"\w+", phrase.lower())
    color_parts: list[str] = []
    while tokens and tokens[0] in _KNOWN_COLORS:
        color_parts.append(tokens.pop(0))
    color = " ".join(color_parts)
    item = "_".join(tokens) if tokens else phrase.lower().replace(" ", "_")
    return color, item


_PREFIX_STOPWORDS: frozenset[str] = frozenset({
    "focus", "on", "the", "a", "an", "her", "his", "their", "its",
    "with", "wearing", "in", "of", "and", "to",
})


def _merge_descriptor_tokens(a: str, b: str) -> str:
    """Combine two descriptor strings, keeping each unique token in
    first-seen order. `_merge_descriptor_tokens('blue', 'highleg')` →
    'blue highleg'. `_merge_descriptor_tokens('blue', 'blue highleg')`
    → 'blue highleg' (no duplicate)."""
    seen: set[str] = set()
    out: list[str] = []
    for token in f"{a} {b}".split():
        key = token.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(token)
    return " ".join(out)


def _extract_color_item_for_slot(phrase: str, slot: str) -> tuple[str, str]:
    """Slot-aware extraction. Find the slot's keyword position in `phrase`
    and capture every word between phrase-start and the keyword as the
    descriptor prefix (color, qualifiers, fabric, etc.) — dropping any
    leading stop-words first.

    Examples:
      "red socks"            → ("red", "socks")
      "black combat boots"   → ("black combat", "boots")
      "small garrison cap"   → ("small garrison", "cap")
      "focus on red socks"   → ("red", "socks")     # 'focus on' stripped
      "her blue leotard"     → ("blue", "leotard")  # 'her' stripped

    Returns ("", "") if the keyword isn't found (caller should skip)."""
    tokens = re.findall(r"\w+", phrase.lower())
    keyword_idx = -1
    for i, tok in enumerate(tokens):
        if tok in _SLOT_KEYWORDS and _SLOT_KEYWORDS[tok] == slot:
            keyword_idx = i
            break
    if keyword_idx == -1:
        return "", ""
    item = tokens[keyword_idx]
    prefix = tokens[:keyword_idx]
    while prefix and prefix[0] in _PREFIX_STOPWORDS:
        prefix.pop(0)
    return " ".join(prefix), item


# ── parser: user_request → deltas ───────────────────────────────────


async def parse_request_via_decompose(user_request: str,
                                      bios: list[dict] | None,
                                      current_state: Optional[PromptState],
                                      decompose_fn,
                                      style_resolver=None,
                                      pose_chip_picker_fn=None,
                                      ) -> list[Delta]:
    """Plan-aligned parser: LLM decomposes user_request into per-section
    sub-intents first; each sub-intent runs through a section-scoped
    parser that only emits deltas for ITS section. Bleed becomes
    structurally impossible — the pose parser never sees outfit text,
    the outfit parser never sees pose text.

    decompose_fn: async callable (user_request: str) -> list[{text, section}].
    Sections: 'outfit' | 'strip' | 'pose' | 'expression' | 'setting' |
    'character' | 'style'.

    Raises DecomposeUnavailable when the LLM decompose returns nothing
    useful (ollama unreachable, model misbehaving, parse error). The
    AI assistant cannot run without a working decompose — caller is
    responsible for turning this into a clear error to the user. We
    DO NOT fall back to regex-driven decomposition, because that would
    silently produce wrong output instead of a clear failure."""
    if not user_request or not user_request.strip():
        return []

    try:
        sub_intents = await decompose_fn(user_request) if decompose_fn else None
    except Exception:
        sub_intents = None

    if not sub_intents:
        raise DecomposeUnavailable(
            "LLM decompose returned nothing — ollama unreachable or the "
            "model misbehaved. The AI assistant requires a working decompose; "
            "we don't fake it with regex."
        )

    all_deltas: list[Delta] = []
    pose_phrases: list[str] = []
    pose_anchor_override = False
    subject_tags: set[str] = set()

    # Multi-pose-intent chip resolution. When 8B decompose splits a single
    # user pose into several sub-intents (`legs up` + `at viewer` from
    # `legs up and blue socks at viewer`), each narrow sub-intent on its
    # own can't anchor a chip cleanly. Pre-loop: gather all pose-section
    # texts + outfit-section texts and run ONE chip lookup. If it hits,
    # the chip's natlang anchors the whole pose; per-sub-intent handling
    # is suppressed. If it misses, fall through to per-sub-intent bio
    # lookup → narrow chip lookup → freeform path.
    #
    # Genuinely-composite intents (`standing AND arms behind head`) tend
    # to land off-band on bucket_search (no single chip covers both),
    # below threshold → chip miss → per-sub-intent freeform. The pre-loop
    # path only short-circuits when ONE chip cleanly covers the intent.
    pose_intent_count = sum(
        1 for si in sub_intents
        if isinstance(si, dict)
        and (si.get("section") or "").strip().lower() == "pose"
        and (si.get("text") or "").strip()
    )
    # Chip-lookup gate: fire whenever decompose emitted a pose sub-intent
    # OR the full user_request shows presentation intent (clothing/body-
    # part + presentation cue). The OR catches the case where decompose
    # mis-routes pose phrasing entirely — without the OR, "showing red
    # socks at viewer" routed by decompose into outfit-only would never
    # trigger chip lookup. Decompose-driven and full-request-driven
    # signals are both valid entry points; either suffices.
    presentation_region_for_gate = _detect_presentation_region(user_request)
    combined_pose_chip: Optional[dict] = None
    if pose_intent_count >= 1 or presentation_region_for_gate:
        pose_parts: list[str] = []
        ctx_parts: list[str] = []
        for si in sub_intents:
            if not isinstance(si, dict):
                continue
            sec = (si.get("section") or "").strip().lower()
            txt = (si.get("text") or "").strip()
            if not txt:
                continue
            if sec == "pose":
                pose_parts.append(txt)
            elif sec in ("outfit", "strip"):
                ctx_parts.append(txt)
        # Bio pose borrow check — only consult when decompose surfaced
        # pose sub-intents to scan. Bio poses are character-named
        # ("victory pose"), so they're typically in pose: subs already.
        any_bio_hit = False
        if pose_parts:
            from .ai_request_parser import _collect_pose_names
            pose_lookup_for_check = _collect_pose_names(bios or [])
            for pose_text in pose_parts:
                if _lookup_bio_pose_in_text(pose_text, pose_lookup_for_check, bios):
                    any_bio_hit = True
                    break
        if not any_bio_hit:
            # Deterministic first: clothing/body-part keyword + presentation
            # cue → body region → curated slot_modifier canonical → bge
            # top hit on that canonical. Same input always picks the same
            # chip. Bypasses LLM picker variance for cases the user's
            # curated taxonomy can resolve unambiguously.
            #
            # E.g. "blue socks at viewer": socks → feet region; "at viewer"
            # → presentation cue; feet region → presenting_foot modifier;
            # bucket_search("presenting foot") → presenting_feet chip.
            # Reuse the gate-detected region; avoid re-scanning.
            if presentation_region_for_gate:
                combined_pose_chip = _lookup_pose_chip_for_region(
                    presentation_region_for_gate,
                )
            if combined_pose_chip is None and pose_intent_count <= 1:
                # LLM-arbitrated fallback — only for SINGLE pose intent.
                # Compound pose intents ("standing AND arms behind head")
                # decompose into multiple pose sub-intents; chip apply
                # would pick one and drop the rest. With <= 1, freeform
                # compose handles compound intent via FRUIT.
                # bge runs on the FULL user_request (not just decompose's
                # pose sub-intent text) so non-presentation pose intent
                # ("kicking", "lying down") gets caught even when
                # decompose routes the pose phrase to a different
                # section. The deterministic foot-presentation region
                # path above is unconstrained — it fires regardless of
                # pose_intent_count because socks+at-viewer always
                # implies presenting_feet, even mid-compound.
                combined_pose_chip = await _lookup_pose_chip_llm_picked(
                    user_request, pose_chip_picker_fn,
                    picker_context=user_request,
                )
    if combined_pose_chip is not None:
        all_deltas.append(ApplyPoseChipDelta(
            chip_tag=combined_pose_chip.get("item_tag") or "",
            display_name=combined_pose_chip.get("display_name") or "",
            base_natlang=combined_pose_chip.get("base_natlang") or "",
            base_tags=combined_pose_chip.get("base_tags") or "",
        ))

    for si in sub_intents:
        if not isinstance(si, dict):
            continue
        text = (si.get("text") or "").strip()
        section = (si.get("section") or "").strip().lower()
        action = (si.get("action") or "").strip().lower()
        if not text:
            continue
        # Typed outfit actions — decompose commits to a specific delta
        # class via `outfit-<action>:` lines, so we dispatch directly
        # without re-parsing. Falls through to the generic outfit
        # parser only when action is missing or unknown.
        if section == "outfit" and action == "remove":
            slot = _resolve_slot_from_phrase(text)
            if slot:
                all_deltas.append(ClearSlotDelta(slot=slot))
            continue
        if section == "outfit" and action == "modifier":
            # The text is the canonical modifier name (barefoot,
            # topless, nude, etc.). Look up its clears_slots from the
            # slot_modifiers table and emit ApplyModifierDelta.
            canon = text.strip().lower().replace(" ", "_")
            mod_row = _lookup_slot_modifier_by_canonical(canon)
            if mod_row is not None:
                all_deltas.append(ApplyModifierDelta(
                    canonical=mod_row["canonical_tag"],
                    clears_slots=list(mod_row.get("clears_slots") or []),
                    substitute_section=(mod_row.get("substitute_section") or ""),
                ))
            continue
        if section == "outfit" and action == "swap":
            # Bio outfit name — look up via _collect_outfit_names.
            from .ai_request_parser import _collect_outfit_names
            lookup = _collect_outfit_names(bios or [])
            hit = lookup.get(text.lower())
            if not hit:
                # Substring fallback (handles "killer bee outfit" → "Killer Bee")
                for name_lc, candidate in lookup.items():
                    if name_lc in text.lower() or text.lower() in name_lc:
                        hit = candidate
                        break
            if hit:
                all_deltas.append(SwapOutfitDelta(
                    outfit_name=hit.get("outfit_name") or "",
                    outfit_id=hit.get("outfit_id"),
                    bio_slots=list(hit.get("slots") or []),
                    bio_natlang=hit.get("natlang") or "",
                    source_character_display=_bio_display_for_tag(
                        hit.get("character_tag") or "", bios or [],
                    ),
                ))
            else:
                # Unknown outfit name — fall through to legacy parser
                # which has a DB re-pick fallback via _pick_outfit_for.
                all_deltas.extend(_parse_outfit_text(text, bios, current_state))
            continue
        if section == "outfit" and action == "fill":
            # Specific slot fill — same path as untyped outfit subintent
            # for fill semantics. Let the slot-aware parser run.
            all_deltas.extend(_parse_outfit_text(text, bios, current_state))
            continue
        if (section == "outfit" and action == "strip") or section == "strip":
            # qwen says "user wants ONLY <text>". Resolve the kept item to
            # a slot, emit StripDelta(kept=[slot]) + FillSlotDelta(item).
            slot = _resolve_slot_from_phrase(text)
            if slot:
                color, item = _extract_color_item_for_slot(text, slot)
                if not item:
                    color, item = _split_color_item(text)
                all_deltas.append(StripDelta(kept_slots=[slot]))
                all_deltas.append(FillSlotDelta(
                    slot=slot, item=item, color=color, origin=ORIGIN_USER,
                ))
            else:
                # Phrase doesn't resolve to a known slot (e.g. user said
                # "only her hat" but bio outfit has no hat). Strip
                # everything and let the user_extras path handle it on
                # next iteration.
                all_deltas.append(StripDelta(kept_slots=[]))
            continue
        if section == "outfit":
            # Untyped outfit subintent — legacy parser handles modifier
            # alias detection, fill extraction, and swap fallback.
            all_deltas.extend(_parse_outfit_text(text, bios, current_state))
            continue
        elif section == "pose":
            # Pre-loop chip resolution already applied a combined-pose
            # chip — skip per-sub-intent handling so we don't add narrow
            # phrases like "at viewer" as descriptive facts that would
            # defeat the chip's render short-circuit.
            if combined_pose_chip is not None:
                continue
            # Cross-character pose borrow: scan ALL bios' matched_pose
            # entries for a name match. `tifa lockhart in cammy white
            # victory pose` → primary is tifa, but bios includes cammy
            # whose matched_pose is Victory Pose; lookup hits, emit
            # SwapPoseDelta to load cammy's bio prose onto tifa's pose
            # state. Mirrors the outfit cross-borrow path that already
            # works through _collect_outfit_names.
            from .ai_request_parser import _collect_pose_names
            pose_lookup = _collect_pose_names(bios or [])
            pose_hit = _lookup_bio_pose_in_text(text, pose_lookup, bios)
            if pose_hit:
                all_deltas.append(SwapPoseDelta(
                    pose_name=pose_hit.get("pose_name") or "",
                    pose_id=pose_hit.get("pose_id"),
                    bio_natlang=pose_hit.get("natlang") or "",
                    is_signature=bool(pose_hit.get("is_signature")),
                    source_character_display=_bio_display_for_tag(
                        pose_hit.get("character_tag") or "", bios or [],
                    ),
                ))
            else:
                phrases, anchor = _parse_pose_text(text, bios, current_state)
                pose_phrases.extend(phrases)
                pose_anchor_override = pose_anchor_override or anchor
        elif section == "expression":
            all_deltas.append(SetExpressionDelta(text=text))
        elif section == "setting":
            all_deltas.append(SetSettingDelta(text=text))
        elif section == "clear":
            # `remove scene` / `no expression` / `clear the pose` →
            # clear the named section. Decompose normalizes to
            # `clear: <section_name>`. Map to the right ClearXDelta
            # so the user's intent ("get rid of this part") doesn't
            # become a SetX with the literal phrase as the value.
            target = re.sub(r"^(?:the\s+)?", "", text.strip().lower()).strip()
            if target in ("setting", "scene", "background", "environment"):
                all_deltas.append(ClearSettingDelta())
            elif target in ("expression", "face", "facial expression", "facial affect"):
                all_deltas.append(ClearExpressionDelta())
            elif target in ("pose", "stance", "action", "pose, action & prop"):
                all_deltas.append(ClearPoseDelta())
            elif target == "style":
                all_deltas.append(ClearStyleDelta())
            # Other targets (e.g. `clear: outfit`) are unhandled here —
            # outfit clear maps to a strip semantics which is its own
            # decompose category.
        elif section == "character":
            char_delta = _parse_character_text(text, bios, current_state)
            if char_delta is not None:
                all_deltas.append(char_delta)
            # Track this character as a SUBJECT regardless of whether
            # the swap delta fired. Subjects render as // Character +
            # // Outfit sections; sources (mentioned only in pose:/
            # outfit: borrow phrases) stay queryable in bios but don't
            # render. Without this, every character in bios would emit
            # subject sections — `cammy white in chun-li's outfit`
            # would render BOTH Cammy and Chun-Li as subjects when only
            # Cammy is meant to be the subject.
            tag = _resolve_subject_tag(text, bios)
            if tag:
                subject_tags.add(tag)
        elif section == "style":
            # Style sub-intents resolve to a SwapStyleDelta via the
            # caller-supplied resolver (which knows about arch_prompts).
            # If no resolver, just stash the name; the post-pass alias
            # scan will run on full user_request as fallback.
            if style_resolver is not None:
                resolved = style_resolver(text)
                if resolved:
                    all_deltas.append(SwapStyleDelta(
                        template_id=resolved.get("id") or "",
                        name=resolved.get("name") or text,
                    ))
                else:
                    all_deltas.append(SwapStyleDelta(
                        template_id="", name=text,
                    ))
            else:
                all_deltas.append(SwapStyleDelta(
                    template_id="", name=text,
                ))

    if pose_phrases:
        all_deltas.append(PoseChangeDelta(
            replaces=[],
            adds=pose_phrases,
            is_anchor_override=pose_anchor_override,
            replaces_all=True,
        ))

    # Source character demotion: when a SwapPose/SwapOutfit delta has a
    # source_character_display set, that character is being borrowed
    # FROM, not used as a subject. Remove from subject_tags. Catches the
    # 8B-decompose false positive where the model emits a `character:`
    # sub-intent for a name that's actually a borrow source ("tifa
    # lockhart in cammy white victory pose" → decompose returns both
    # tifa and cammy as `character:`; cammy is the pose source, not a
    # subject).
    source_displays = {
        (d.source_character_display or "").strip().lower()
        for d in all_deltas
        if isinstance(d, (SwapPoseDelta, SwapOutfitDelta))
        and (getattr(d, "source_character_display", "") or "").strip()
    }
    source_displays.discard("")
    if source_displays and bios:
        source_tags = {
            (b.get("tag") or "").lower()
            for b in bios
            if (b.get("display") or "").strip().lower() in source_displays
        }
        source_tags.discard("")
        # Only demote a source character if there's at least one OTHER
        # subject — otherwise borrowing pose/outfit from a character
        # without a separate subject means the source IS the subject.
        if source_tags and (subject_tags - source_tags):
            subject_tags -= source_tags

    # Subject pruning: when the user explicitly named ANY characters
    # via `character:` sub-intents (after source demotion above),
    # restrict state.characters to that set. Other matched bios stay
    # available for cross-borrow lookups via the bios parameter, they
    # just don't render as subjects. When subject_tags is empty (no
    # explicit character mention), leave state.characters alone —
    # typical of "killer bee outfit" with a single matched character
    # preloaded by match-characters.
    if subject_tags and current_state and len(current_state.characters) > 1:
        current_state.characters = [
            c for c in current_state.characters
            if (c.tag or "").lower() in subject_tags
        ]

    all_deltas.sort(key=_delta_priority)
    return all_deltas


def _resolve_subject_tag(text: str, bios: list[dict] | None) -> str:
    """Find the bio tag whose name appears in `text`. Used to track
    SUBJECT characters from `character:` decompose sub-intents — only
    bios that match here render as // Character + // Outfit blocks.

    Separator-tolerant: matches `chun_li` to `chun-li` and vice versa."""
    if not text or not bios:
        return ""
    text_lc = text.strip().lower()
    if not text_lc:
        return ""
    text_norm = re.sub(r"[_\-]+", " ", text_lc).strip()
    for bio in bios:
        tag = (bio.get("tag") or "").lower()
        display = (bio.get("display") or "").lower()
        if not tag:
            continue
        names = {tag, display, re.sub(r"[_\-]+", " ", tag).strip(),
                 re.sub(r"[_\-]+", " ", display).strip()}
        names.discard("")
        if any(name and name in text_norm for name in names):
            return tag
    return ""


def _parse_outfit_text(text: str,
                       bios: list[dict] | None,
                       current_state: Optional[PromptState]) -> list[Delta]:
    """Parse a sub-intent text scoped to the outfit section. Emits only
    outfit-domain deltas: SwapOutfit / Strip / FillSlot / ApplyModifier
    (where the modifier's substitute_section is outfit or unset).

    Decompose may return outfit names without the `in X outfit` framing —
    e.g. raw `killer bee outfit` or even bare `killer bee`. If the
    regex-based parser misses a swap, we look up the text against the
    bio outfit cache and emit a SwapOutfitDelta directly."""
    from .ai_request_parser import (
        parse_intents as _parse_intents, _collect_outfit_names,
    )
    parsed = _parse_intents(text, bios or [])
    intents = parsed.get("intents") or []
    has_swap = any(i.get("kind") == "outfit_swap" for i in intents)

    out: list[Delta] = []
    # Decompose-aware swap: if no swap intent fired but the text mentions
    # the word "outfit"/"costume"/"attire" or matches a known bio outfit
    # name, emit a swap delta.
    if not has_swap:
        outfit_lookup = _collect_outfit_names(bios or [])
        text_lc = text.strip().lower()
        # Strip leading "in "/"the "/"her "/"his " and trailing
        # outfit-anchor word.
        stripped = re.sub(
            r"^\s*(?:in\s+|wearing\s+)?(?:the\s+|her\s+|his\s+)?",
            "", text_lc,
        )
        stripped = re.sub(
            r"\s+(?:outfit|costume|attire)\s*$", "", stripped,
        ).strip()
        # Strip possessive `'s` / `'s` so `chun-li's outfit` → `chun-li's`
        # → `chun-li` matches the bio's character tag. Both straight and
        # curly apostrophes covered.
        stripped = re.sub(r"['’]s\s*$", "", stripped).strip()
        hit = None
        if stripped in outfit_lookup:
            hit = outfit_lookup[stripped]
        else:
            for name_lc, candidate in outfit_lookup.items():
                if name_lc in stripped or stripped in name_lc:
                    hit = candidate
                    break
        # Character-name borrow: `cammy white in the chun-li outfit` —
        # `chun-li` is a CHARACTER name, not an outfit name. The user
        # means "borrow whatever outfit chun-li's bio has loaded". Look
        # up against bio tags / display names; on hit, take that bio's
        # currently-loaded outfit (user_requested_outfit > default_outfit).
        if not hit and stripped:
            hit = _lookup_character_outfit(stripped, bios or [])
        if hit:
            out.append(SwapOutfitDelta(
                outfit_name=hit.get("outfit_name") or "",
                outfit_id=hit.get("outfit_id"),
                bio_slots=list(hit.get("slots") or []),
                bio_natlang=hit.get("natlang") or "",
                source_character_display=_bio_display_for_tag(
                    hit.get("character_tag") or "", bios or [],
                ),
            ))
            has_swap = True

    for intent in intents:
        kind = intent.get("kind")
        if kind == "outfit_swap":
            out.append(SwapOutfitDelta(
                outfit_name=intent.get("outfit_name") or "",
                outfit_id=intent.get("outfit_id"),
                bio_slots=list(intent.get("slots") or []),
                bio_natlang=intent.get("natlang") or "",
                source_character_display=_bio_display_for_tag(
                    intent.get("character_tag") or "", bios or [],
                ),
            ))
        elif kind == "outfit_strip":
            target = (intent.get("target_phrase") or "").strip()
            slot = _resolve_slot_from_phrase(target) if target else None
            if slot:
                color, item = _extract_color_item_for_slot(target, slot)
                out.append(StripDelta(kept_slots=[slot]))
                out.append(FillSlotDelta(
                    slot=slot, item=item, color=color, origin=ORIGIN_USER,
                ))
            else:
                out.append(StripDelta(kept_slots=[]))
        elif kind == "modifier_apply":
            mod = intent.get("modifier") or {}
            section = (mod.get("substitute_section") or "").strip().lower()
            if section in ("", "outfit"):
                out.append(ApplyModifierDelta(
                    canonical=(mod.get("canonical_tag") or "").lower(),
                    clears_slots=list(mod.get("clears_slots") or []),
                    substitute_section=section,
                ))
        # Pose / expression / setting / style intents from this text get
        # silently dropped — decompose says this text is outfit-scoped, so
        # a stray pose intent here is decompose noise, not a real signal.
    out.extend(_extract_fill_deltas_from_residue(parsed.get("descriptive_residue") or ""))
    return out


def _bio_display_for_tag(tag: str, bios: list[dict]) -> str:
    """Look up a character's display name from bios by tag. Empty when
    not found. Used to attribute borrowed outfits/poses in the rendered
    section header (`from Character: <Display>`)."""
    if not tag or not bios:
        return ""
    tag_lc = tag.lower()
    for bio in bios:
        if (bio.get("tag") or "").lower() == tag_lc:
            return (bio.get("display") or bio.get("tag") or "").strip()
    return ""


def _lookup_character_outfit(text: str, bios: list[dict]) -> Optional[dict]:
    """When the outfit sub-intent text is a CHARACTER name (e.g. user said
    `cammy white in the chun-li outfit`), look up that character in bios
    and return their currently-loaded outfit data (user_requested_outfit
    if present, else default_outfit). The returned dict has the same
    shape _collect_outfit_names entries do — outfit_name / outfit_id /
    slots / natlang — so the caller emits SwapOutfitDelta uniformly.

    Matches against bio's `tag`, `display`, and space/dash/underscore
    variants. `chun-li` matches `chun-li`; `chun li` matches the same;
    `tifa lockhart` matches `tifa_lockhart`."""
    if not text or not bios:
        return None
    norm = re.sub(r"[\s_\-]+", " ", text.strip().lower()).strip()
    if not norm:
        return None
    for bio in bios:
        tag = (bio.get("tag") or "").lower()
        display = (bio.get("display") or "").lower()
        candidates = {
            tag,
            display,
            re.sub(r"[\s_\-]+", " ", tag).strip(),
            re.sub(r"[\s_\-]+", " ", display).strip(),
        }
        candidates.discard("")
        if norm not in candidates:
            continue
        outfit = bio.get("user_requested_outfit") or bio.get("default_outfit")
        if not outfit:
            return None
        return {
            "character_tag": tag,
            "outfit_name": outfit.get("name") or "",
            "outfit_id": outfit.get("id"),
            "slots": list(outfit.get("slots") or []),
            "natlang": outfit.get("natlang") or "",
            "tags": outfit.get("tags") or "",
        }
    return None


def _parse_pose_text(text: str,
                     bios: list[dict] | None,
                     current_state: Optional[PromptState]
                     ) -> tuple[list[str], bool]:
    """Parse a sub-intent text scoped to the pose section. Returns the
    phrases to add and whether the anchor should be overridden.

    Pose parsing is deliberately liberal — anything in this scoped text
    is by definition pose-relevant (decompose said so). Posture verbs
    set anchor_override; non-verb phrases like 'with legs up' just
    contribute the phrase as a fact.

    Bio-pose-name short-circuit: when the sub-intent text is just the
    user naming the bio's matched pose (e.g. `victory pose`,
    `the victory pose`), don't add anything — the bio anchor is already
    loaded into state and will short-circuit during render. Adding the
    name as a descriptive_fact would force the LLM compose path, which
    treats facts as truth and ignores the rich anchor.

    Trailing dangling prepositions (`with`, `in`, `on`, `at`, ...) get
    stripped — they appear when the segmenter cut off a noun that the
    next section consumed (e.g. `with barefeet` → barefoot becomes an
    outfit segment, leaving `with` dangling on the pose segment)."""
    from .ai_request_parser import _POSTURE_VERBS, _word_in
    raw = text.strip().rstrip(".,;")
    raw = re.sub(r"\s+(?:with|in|at|on|by|to|from)\s*$", "", raw, flags=re.IGNORECASE).strip(" ,.;:")
    if not raw:
        return [], False
    if _matches_bio_pose_name(raw, current_state):
        return [], False
    raw_lc = raw.lower()
    is_posture = any(_word_in(raw_lc, v) for v in _POSTURE_VERBS)
    return [raw], is_posture


_POSE_NAME_NOISE_RE = re.compile(r"\b(?:pose|stance)\b", re.IGNORECASE)
_POSE_NAME_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")
_POSE_NAME_LEAD_RE = re.compile(
    r"^(?:doing\s+|the\s+|a\s+|an\s+|her\s+|his\s+|in\s+|with\s+)+",
    re.IGNORECASE,
)
# Calibrated against Cammy's 8 poses. There is a clean gap between
# genuine paraphrases and broadly-related vocabulary:
#   "fighting"   → Combat Stance     0.847   ← accept (paraphrase)
#   "salute"     → Military Salute   0.897   ← accept (synonym)
#   "sparring"   → Combat Stance     0.742   ← reject (action between chars,
#                                              not a single-pose name; the
#                                              existing multi_char_sparring
#                                              fixture relies on this falling
#                                              through to PoseChangeDelta)
#   "kicking"    → Combat Stance     0.660   ← reject (ambiguous across
#                                              Cannon Spike/Strike)
#   "rolling"    → Combat Stance     0.633   ← reject (correct: should be
#                                              Hooligan Combination but the
#                                              embedding can't see that)
# Threshold 0.78 sits in the gap. Margin floor stays small because the
# threshold itself does most of the discrimination work.
_POSE_SEMANTIC_THRESHOLD = 0.78
_POSE_SEMANTIC_MIN_MARGIN = 0.05


def _strip_pose_name_noise(name: str) -> str:
    s = _POSE_NAME_PARENS_RE.sub(" ", name)
    s = _POSE_NAME_NOISE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _semantic_pose_match(query_clean: str,
                          candidate_names: list[str]) -> int:
    """Cosine-pick the candidate whose stripped name best matches
    `query_clean` via bge-small. Returns the index above threshold, or
    -1 when below threshold / model unavailable / margin too tight.

    Embed strategy: name only (no natlang body, no tags). The bodies for
    different poses on the same character share too much vocabulary
    (kicks, fighting, military, etc.) which dilutes the signal. Names
    are short and curated — embedded against a paraphrased query they
    surface a sharp match when one exists and gracefully fall below
    threshold otherwise."""
    if not query_clean or not candidate_names:
        return -1
    try:
        from . import _embed_model
    except Exception:
        return -1
    if _embed_model.get() is None:
        return -1
    cleaned_candidates = [_strip_pose_name_noise(n) for n in candidate_names]
    if not any(cleaned_candidates):
        return -1
    cand_embs = _embed_model.embed(cleaned_candidates)
    q_emb = _embed_model.embed([query_clean])
    if cand_embs is None or q_emb is None:
        return -1
    scores = (q_emb @ cand_embs.T).cpu().squeeze(0).tolist()
    if not scores:
        return -1
    if isinstance(scores, float):
        scores = [scores]
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    top_i = ranked[0]
    top_s = scores[top_i]
    if top_s < _POSE_SEMANTIC_THRESHOLD:
        return -1
    if len(ranked) >= 2:
        margin = top_s - scores[ranked[1]]
        if margin < _POSE_SEMANTIC_MIN_MARGIN:
            return -1
    return top_i


def _lookup_bio_pose_in_text(text: str, pose_lookup: dict,
                              bios: list[dict] | None = None) -> Optional[dict]:
    """Find a bio pose whose name appears in `text`. Strips the user's
    leading verbs/articles ('doing', 'the', 'a', 'in') and trailing
    'pose' before comparing — `doing the cammy white victory pose` →
    `cammy white victory` core, then we substring-match against each
    lookup key's stripped form.

    When `bios` is supplied AND `text` mentions a character name (any
    bio's tag/display, separator-tolerant), the search is SCOPED to
    that character's poses first. Without this, `chun-li victory pose`
    falsely matches Cammy's `Victory Pose (Rear)` because both share
    the "victory" substring and iteration order picks the wrong one.

    Falls back to bge-small semantic match against pose names when
    substring fails — `fighting pose` resolves to `Combat Stance` even
    though neither string contains the other.

    Returns the lookup entry or None. The dict shape mirrors
    _collect_pose_names: {character_tag, pose_name, pose_id,
    is_signature, natlang, tags}."""
    if not text or not pose_lookup:
        return None
    cleaned = _POSE_NAME_LEAD_RE.sub("", text.strip().lower()).strip()
    cleaned = re.sub(r"\s+pose\s*$", "", cleaned).strip()
    if not cleaned:
        return None

    # Character-scoped first pass: if the text names a character, only
    # consider that character's poses. Pose names commonly collide
    # across characters ("Victory Pose" appears for many), so without
    # scoping we'd routinely pick the wrong character's pose.
    scoped_tag = _resolve_subject_tag(text, bios) if bios else ""
    scoped_lookup = None
    if scoped_tag:
        scoped_lookup = {
            k: v for k, v in pose_lookup.items()
            if (v.get("character_tag") or "").lower() == scoped_tag
        }

    def _substring_match(lookup: dict) -> Optional[dict]:
        for name_lc, entry in lookup.items():
            bio_core = _strip_pose_name_noise(name_lc)
            if not bio_core:
                continue
            if cleaned == bio_core or bio_core in cleaned:
                return entry
        return None

    if scoped_lookup:
        hit = _substring_match(scoped_lookup)
        if hit:
            return hit
        # Semantic fallback inside scope before broadening.
        keys = list(scoped_lookup.keys())
        cleaned_query = _POSE_NAME_NOISE_RE.sub(" ", cleaned).strip()
        best_i = _semantic_pose_match(cleaned_query, keys)
        if best_i >= 0:
            return scoped_lookup[keys[best_i]]
        # Fall through to full search if scoped lookup missed entirely.

    hit = _substring_match(pose_lookup)
    if hit:
        return hit
    keys = list(pose_lookup.keys())
    cleaned_query = _POSE_NAME_NOISE_RE.sub(" ", cleaned).strip()
    best_i = _semantic_pose_match(cleaned_query, keys)
    if best_i >= 0:
        return pose_lookup[keys[best_i]]
    return None


# Confidence threshold for auto-applying a generic pose chip. Calibrated
# against fixture telemetry: adjusted_score typically lands 0.75+ for
# coherent chip matches and < 0.65 for off-topic queries. 0.65 is
# permissive — when the chip pool genuinely covers the user intent, the
# chip wins; ambiguous low-cosine hits fall back to freeform compose.
# Compound pose intents (multiple pose sub-intents this turn) bypass
# chip apply entirely — see _parse_request_via_decompose loop.
_POSE_CHIP_THRESHOLD = 0.65

# Bucket-search buckets that map to the pose section. Action chips
# (boxing, climbing, dancing, etc.) sit alongside pose chips in v2 — same
# section header. nsfw_action chips (footjob, sex positions, etc.) only
# join the retrieval pool when the user_request carries an explicit NSFW
# signal — see `_detect_nsfw_intent`. Without a signal a query like
# "wearing red socks showing them at camera" would surface
# `presenting_foot` (an nsfw_action whose natlang says "bare foot")
# over the curated SFW `presenting_feet` pose chip, breaking content
# consistency with the user's stated outfit.
_POSE_CHIP_BUCKETS_SFW = ("pose", "action")
_POSE_CHIP_BUCKETS_NSFW = ("pose", "action", "nsfw_action")

# Lower-case word-boundary matches against user_request. Curated for
# strong signal — words like "showing" / "spreading" / "presenting" on
# their own are NOT NSFW (they describe pose, not sex acts); inclusion
# would defeat the whole point of the filter.
_NSFW_INTENT_WORDS: frozenset[str] = frozenset({
    "nude", "naked", "sex", "sexual", "fuck", "fucking", "fucked",
    "penis", "cock", "dick", "pussy", "vagina", "vulva", "clit",
    "anal", "anus", "asshole", "blowjob", "fellatio", "cunnilingus",
    "rimjob", "handjob", "footjob", "titjob", "masturbating",
    "masturbation", "fingering", "orgasm", "cum", "cumshot", "creampie",
    "ejaculation", "ejaculating", "semen", "squirting", "squirt",
    "nipple", "nipples", "areola", "areolae", "tits", "boobs",
    "breast_grab", "ass_grab", "spread_pussy", "spread_anus",
    "tentacle", "bukkake", "gangbang", "bondage", "bdsm",
    "domination", "submissive", "slave", "leash",
    "dildo", "vibrator", "buttplug", "buttplugged",
})


def _detect_nsfw_intent(text: str) -> bool:
    """True when user_request has an explicit NSFW token. Stays
    conservative — words shared between SFW pose talk and NSFW content
    ('spread', 'showing') are NOT signals. Only unambiguous tokens flip
    the bit so the nsfw_action bucket joins chip retrieval."""
    if not text:
        return False
    lower = text.lower()
    for w in _NSFW_INTENT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", lower):
            return True
    return False


_STOPWORDS_FOR_POSE_LOOKUP: frozenset[str] = frozenset({
    "a", "an", "the", "her", "his", "their", "its", "with", "in", "on",
    "at", "of", "to", "from", "and", "or", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "for", "by", "as", "while", "this", "that", "these", "those",
    "viewer", "camera",
})


def _lookup_pose_chip_in_text(text: str) -> Optional[dict]:
    """Semantic-search the generic chip pool for a pose / action / nsfw
    chip whose authored prose matches `text`. Returns the top hit above
    _POSE_CHIP_THRESHOLD, or None.

    Two-stage retrieval to mirror tag mode's "semantic + literal anchor"
    pattern:
      1. bge-small variant-free cosine retrieval via
         bucket_search.search_for_apply → candidate pool filtered to
         pose-side buckets.
      2. Lexical re-rank within the top semantic band: count non-stop-
         word user tokens that appear in the chip's display_name +
         base_tags + base_natlang. Ties broken by adjusted_score.

    Why re-rank: bge can't distinguish 'feet up' from 'presenting feet'
    by cosine alone — both share 'feet' and a body-part region. The
    user's verbatim word choice is the strongest signal of intent,
    and the chip whose authored prose contains MORE of those words is
    by construction the right chip. Same shape as tag mode's literal
    anchor pass over danbooru_tags."""
    if not text or not text.strip():
        return None
    try:
        from . import bucket_search
    except Exception:
        return None
    buckets = (
        _POSE_CHIP_BUCKETS_NSFW if _detect_nsfw_intent(text)
        else _POSE_CHIP_BUCKETS_SFW
    )
    try:
        hits = bucket_search.search_for_apply(text, buckets=buckets, top_k=15)
    except Exception:
        return None
    if not hits:
        return None
    top_score = hits[0].get("adjusted_score") or 0.0
    if top_score < _POSE_CHIP_THRESHOLD:
        return None

    query_tokens = {
        t for t in re.findall(r"\w+", text.lower())
        if t and t not in _STOPWORDS_FOR_POSE_LOOKUP
    }
    if not query_tokens:
        return hits[0]

    def _lexical_score(h: dict) -> int:
        bag = " ".join([
            h.get("display_name") or "",
            h.get("base_tags") or "",
            h.get("base_natlang") or "",
        ]).lower()
        bag_tokens = set(re.findall(r"\w+", bag))
        return sum(1 for t in query_tokens if t in bag_tokens)

    # Combined score: cosine + 0.03 * lexical_overlap. Weight calibrated
    # so each extra literal match adds about as much as a meaningful
    # cosine gap — three matched user words can flip a 0.05 cosine
    # deficit, one matched word adds 0.03 (less than a typical semantic
    # gap so a marginal lexical hit doesn't displace a clear semantic
    # winner). Bridges bge's synonym-blindness for verbatim queries:
    # 'sitting with legs up presenting feet' lexically lights up
    # `presenting_feet` whose natlang contains those exact phrases.
    def _combined(h: dict) -> float:
        return (h.get("adjusted_score") or 0.0) + 0.03 * _lexical_score(h)

    return max(hits, key=_combined)


# Width of the bge candidate pool sent to the LLM picker. 8 is enough for
# the LLM to see meaningfully different options without overflowing its
# attention — empirical sweet spot for 8B-class models. Tighten if pose
# chip count grows enough that 8 routinely truncates the right answer.
_POSE_CHIP_LLM_CANDIDATE_K = 8


def _lookup_pose_chip_for_region(region: str) -> Optional[dict]:
    """Find the curated pose chip whose authored tags + natlang most
    match the slot_modifier canonical for the given body region.

    Maps region → modifier (`feet` → `presenting_foot`), then runs
    bucket_search on the modifier's canonical_tag (e.g. "presenting
    foot"). Top pose-bucket hit wins. Deterministic — same input always
    yields the same chip — and uses only curated metadata (no LLM, no
    chip-specific hardcoding). The user's `slot_modifier` table is the
    bridge between "user typed a presentation intent" and "which chip
    encodes that intent in pose_items"."""
    if not region:
        return None
    modifier_canonical = _REGION_TO_POSE_MODIFIER.get(region)
    if not modifier_canonical:
        return None
    try:
        from . import bucket_search
    except Exception:
        return None
    query = modifier_canonical.replace("_", " ")
    try:
        hits = bucket_search.search_for_apply(
            query, buckets=_POSE_CHIP_BUCKETS_SFW, top_k=5,
        )
    except Exception:
        return None
    if not hits:
        return None
    # Top pose-bucket hit by cosine. The presenting_foot modifier's
    # canonical embeds closely with presenting_feet chip metadata
    # (display_name "Presenting Feet", base_tags contains "presenting
    # feet, soles, foot focus") — bge reliably surfaces it #1 for the
    # short query "presenting foot".
    return hits[0]


async def _lookup_pose_chip_llm_picked(text: str, picker_fn,
                                        picker_context: str = "") -> Optional[dict]:
    """LLM-arbitrated chip pick. bge retrieves top-K candidates from the
    focused pose+outfit context (`text`); the LLM picks among them
    reading the FULL user request (`picker_context`) so it sees the
    presentation cues (`showing`, `at viewer`, `pointing`) that got
    routed to other sub-intents and aren't in the retrieval query.
    Returns the chosen chip or None.

    Mirrors tag mode's architecture: tag mode retrieves candidates over
    `danbooru_tag_wikis` then has the LLM pick via the main patch system
    prompt. We do the same here — bge is a sieve, the LLM is the
    decider. Bridges bge's surface-similarity blindness for queries
    where the user's word choice doesn't lexically overlap with the
    chip metadata ('legs up at viewer' → presenting_feet via LLM
    understanding, not via cosine).

    When picker_fn is None (no LLM available — stub harness, ollama
    down) falls back to the deterministic bge+lexical heuristic so
    development without an LLM still produces reasonable chip picks."""
    if not text or not text.strip():
        return None
    if picker_fn is None:
        return _lookup_pose_chip_in_text(text)
    try:
        from . import bucket_search
    except Exception:
        return None
    buckets = (
        _POSE_CHIP_BUCKETS_NSFW if _detect_nsfw_intent(text)
        else _POSE_CHIP_BUCKETS_SFW
    )
    try:
        hits = bucket_search.search_for_apply(
            text, buckets=buckets, top_k=_POSE_CHIP_LLM_CANDIDATE_K,
        )
    except Exception:
        return None
    if not hits:
        return None
    # Skip the LLM call when bge confidence is uniformly low — no
    # candidate is plausibly the right chip. Saves an LLM round-trip
    # for queries that genuinely don't have a chip match.
    top_score = hits[0].get("adjusted_score") or 0.0
    if top_score < _POSE_CHIP_THRESHOLD:
        return None
    # Picker sees the full user request when available — it carries the
    # presentation cues (`at viewer`, `showing`, `pointing`) that
    # decompose routes away from the pose sub-intent. Falls back to
    # the focused retrieval text when no full request is supplied.
    context_for_picker = picker_context.strip() if picker_context else text
    try:
        chosen = await picker_fn(context_for_picker, hits)
    except Exception:
        return None
    return chosen


def _matches_bio_pose_name(text: str,
                            current_state: Optional[PromptState]) -> bool:
    """True when `text` is just a reference to the bio's matched pose
    name (e.g. user said `victory pose` and bio.matched_pose.name is
    `Victory Pose (Rear)`). Fuzzy: strips leading articles and trailing
    `pose`, then checks substring match against the bio name.

    Restricted to BIO-anchored poses (bio_pose_id set). Chip-anchored
    poses (bio_pose_id=None) don't benefit from this short-circuit:
    the user's new pose phrase isn't a name-reference to the chip
    — it's an additive fact ('spreading toes' alongside the
    'Presenting Feet' chip). Bge cosine between e.g. 'spreading toes'
    and 'Presenting Feet' crosses the semantic threshold and would
    falsely drop the fact."""
    if current_state is None:
        return False
    char = current_state.primary_character()
    if char is None:
        return False
    # Chip-anchored poses skip the name-match drop.
    if char.pose.bio_pose_id is None:
        return False
    bio_name = (char.pose.name or "").strip().lower()
    if not bio_name or not char.pose.natlang_anchor.strip():
        return False
    cleaned = _POSE_NAME_LEAD_RE.sub("", text.strip().lower()).strip()
    cleaned = re.sub(r"\s+pose\s*$", "", cleaned).strip()
    if not cleaned:
        return False
    bio_core = _strip_pose_name_noise(bio_name)
    if not bio_core:
        return False
    if cleaned == bio_core or cleaned in bio_core or bio_core in cleaned:
        return True
    cleaned_query = _POSE_NAME_NOISE_RE.sub(" ", cleaned).strip()
    return _semantic_pose_match(cleaned_query, [bio_name]) == 0


def _parse_character_text(text: str,
                          bios: list[dict] | None,
                          current_state: Optional[PromptState]) -> Optional[Delta]:
    """Parse a sub-intent text scoped to the character section. Returns
    a SwapCharacterDelta when the user names a character.

    Multi-character build mode protection: when state.characters has
    MULTIPLE entries (refresh_state_with_bios loaded them all), and the
    named character is one of them, return None so we don't fire
    SwapCharacter and replace primary with secondary.

    Single-char swap case: when state has exactly one character and the
    user names it, we DO emit SwapCharacterDelta — this is the signal
    that the // Character section changed. Without it,
    changed_sections_from_deltas omits "character" and the renderer
    preserves the OLD character section from node_prompt verbatim. This
    matters because the chat agent's character_queries preflight loads
    only the new character into bios, so state.characters after refresh
    has just the new char — but node_prompt still shows the previous
    one, and only a SwapCharacterDelta unblocks regeneration."""
    if not bios:
        return None
    text_lc = text.strip().lower()
    if not text_lc:
        return None
    # Normalize separators so `chun_li` (user / chat-agent canonical) and
    # `chun-li` (Danbooru tag form) match each other. match-characters
    # already does this on the way in; the natlang sub-intent parser
    # missed it.
    def _norm(s: str) -> str:
        return re.sub(r"[_\-]+", " ", s).strip()
    text_norm = _norm(text_lc)
    multi_char_state = bool(
        current_state and len(current_state.characters) > 1
    )
    loaded_tags: set[str] = set()
    if current_state and multi_char_state:
        for c in current_state.characters:
            tag = (c.tag or "").lower()
            if tag:
                loaded_tags.add(tag)
    for bio in bios:
        bio_tag = (bio.get("tag") or "").lower()
        bio_display = (bio.get("display") or "").lower()
        if not bio_tag:
            continue
        names = {bio_tag, bio_display, _norm(bio_tag), _norm(bio_display)}
        names.discard("")
        if any(name and name in text_norm for name in names):
            if bio_tag in loaded_tags:
                # Multi-char build mode: both already loaded, no swap.
                return None
            return SwapCharacterDelta(
                target_tag=bio_tag,
                target_display=bio.get("display") or bio_tag,
            )
    return None


def parse_user_request_to_deltas(user_request: str,
                                 bios: list[dict] | None,
                                 current_state: Optional[PromptState] = None
                                 ) -> list[Delta]:
    """Walk user_request through the existing intent extractor, then
    convert each intent into the right Delta dataclass. Returns the
    deltas sorted by application order."""
    if not user_request or not user_request.strip():
        return []

    # Lazy import to dodge circular load.
    from .ai_request_parser import parse_intents

    parsed = parse_intents(user_request, bios or [])
    intents = parsed.get("intents") or []

    deltas: list[Delta] = []
    pose_phrases: list[str] = []
    pose_anchor_override = False
    for intent in intents:
        kind = intent.get("kind")
        if kind == "outfit_swap":
            deltas.append(SwapOutfitDelta(
                outfit_name=intent.get("outfit_name") or "",
                outfit_id=intent.get("outfit_id"),
                bio_slots=list(intent.get("slots") or []),
                bio_natlang=intent.get("natlang") or "",
            ))
        elif kind == "outfit_strip":
            target = (intent.get("target_phrase") or "").strip()
            slot = _resolve_slot_from_phrase(target) if target else None
            if slot:
                color, item = _split_color_item(target)
                deltas.append(StripDelta(kept_slots=[slot]))
                deltas.append(FillSlotDelta(
                    slot=slot, item=item, color=color, origin=ORIGIN_USER,
                ))
            else:
                deltas.append(StripDelta(kept_slots=[]))
        elif kind == "modifier_apply":
            mod = intent.get("modifier") or {}
            deltas.append(ApplyModifierDelta(
                canonical=(mod.get("canonical_tag") or "").lower(),
                clears_slots=list(mod.get("clears_slots") or []),
                substitute_section=(mod.get("substitute_section") or "").strip().lower(),
            ))
        elif kind == "pose_swap":
            pose_name = intent.get("pose_name") or ""
            if pose_name:
                pose_phrases.append(pose_name)
                pose_anchor_override = True
        elif kind == "pose_descriptive":
            phrase = intent.get("phrase") or ""
            if phrase:
                pose_phrases.append(phrase)
                if intent.get("is_posture"):
                    pose_anchor_override = True
        elif kind == "expression_set":
            deltas.append(SetExpressionDelta(text=intent.get("expression") or ""))
        elif kind == "expression_descriptive":
            deltas.append(SetExpressionDelta(text=intent.get("phrase") or ""))
        elif kind == "setting_set":
            deltas.append(SetSettingDelta(text=intent.get("setting") or ""))
        elif kind == "setting_descriptive":
            deltas.append(SetSettingDelta(text=intent.get("phrase") or ""))
        elif kind == "style_swap":
            deltas.append(SwapStyleDelta(
                template_id=intent.get("template_id") or "",
                name=intent.get("name") or "",
            ))
        # else: unknown intent kind, skip silently

    # Collapse all this turn's pose intents into ONE PoseChangeDelta with
    # replaces_all=True. Fresh-pose semantics — any turn that mentions
    # pose-relevant text replaces the prior descriptive_facts entirely.
    # Without this, every iteration accumulates the user's pose phrases
    # forever and the section reads as a chain of stale descriptions.
    if pose_phrases:
        deltas.append(PoseChangeDelta(
            replaces=[],
            adds=pose_phrases,
            is_anchor_override=pose_anchor_override,
            replaces_all=True,
        ))

    # Free-form "wearing X" also becomes a FillSlotDelta when the residue
    # contains a clothing keyword. We scan the descriptive_residue for
    # `wearing\s+(...)` patterns the existing parser missed.
    residue = parsed.get("descriptive_residue") or ""
    for fill in _extract_fill_deltas_from_residue(residue):
        deltas.append(fill)

    deltas.sort(key=_delta_priority)
    return deltas


def _extract_fill_deltas_from_residue(residue: str) -> list[FillSlotDelta]:
    """Find leftover `wearing X` / `with X` / bare clothing-keyword phrases
    in the residue and emit FillSlotDelta for each that resolves to a slot.

    Uses slot-aware color/item extraction so `focus on red socks` becomes
    ("red", "socks") for legwear, not ("", "focus_on_red_socks"). The slot
    keyword's position anchors the extraction; everything before any
    leading color is treated as prefix junk and dropped."""
    if not residue:
        return []
    out: list[FillSlotDelta] = []
    seen_slots: set[str] = set()
    for pat in (
        re.compile(r"\bwearing\s+(.+?)(?:\.|,|;|$)", re.IGNORECASE),
        re.compile(r"\bwith\s+(.+?)(?:\.|,|;|$)", re.IGNORECASE),
    ):
        for m in pat.finditer(residue):
            phrase = m.group(1).strip()
            if not phrase:
                continue
            slot = _resolve_slot_from_phrase(phrase)
            if not slot or slot in seen_slots:
                continue
            color, item = _extract_color_item_for_slot(phrase, slot)
            if not item:
                continue
            seen_slots.add(slot)
            out.append(FillSlotDelta(
                slot=slot, item=item, color=color, origin=ORIGIN_USER,
            ))
    if not out:
        slot = _resolve_slot_from_phrase(residue)
        if slot:
            color, item = _extract_color_item_for_slot(residue, slot)
            if item:
                out.append(FillSlotDelta(
                    slot=slot, item=item, color=color, origin=ORIGIN_USER,
                ))
    return out


# Negation cues. When any of these appears within a short window before a
# slot keyword, the keyword is being NEGATED, not described — skip emit.
# `no socks` / `without socks` / `remove socks` / `kicks off her boots`
# / `not wearing gloves` all mean the item is absent.
_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:no|not|without|remove|removed|drop|drops|dropped|stop|stops|"
    r"kicks?\s+off|kicked\s+off|takes?\s+off|took\s+off|threw\s+off|"
    r"none|never)\s+(?:wearing\s+)?\w*\s*\w*\s*$",
    re.IGNORECASE,
)


# Presentation-cue phrases. Word-boundary matched against user_request.
# Curated for high-signal: phrases like `at viewer` / `at camera` /
# `to viewer` / `to the camera` reliably indicate the user wants
# something presented IN-FRAME, regardless of which body part. The
# specific body part is inferred separately from clothing/body-part
# keywords (see `_detect_presentation_region`).
_PRESENTATION_CUE_RE = re.compile(
    r"\b(?:"
    r"at\s+(?:the\s+)?viewer|at\s+(?:the\s+)?camera|"
    r"to\s+(?:the\s+)?viewer|to\s+(?:the\s+)?camera|"
    r"toward(?:s)?\s+(?:the\s+)?viewer|toward(?:s)?\s+(?:the\s+)?camera|"
    r"facing\s+(?:the\s+)?viewer|facing\s+(?:the\s+)?camera|"
    r"showing\s+(?:them|it|her|his|their)|"
    r"pointing\s+(?:them|her|his|their)|"
    r"presenting\s+(?:them|her|his|their)|"
    r"displaying\s+(?:them|her|his|their)|"
    r"point(?:ed|ing)?\s+at\s+(?:the\s+)?viewer|"
    r"point(?:ed|ing)?\s+at\s+(?:the\s+)?camera"
    r")\b",
    re.IGNORECASE,
)

# Clothing keyword → body region. Mirrors `bucket_search._CLOTHING_REGION`
# but lives here so natlang_facts doesn't import bucket_search just for
# this map. Used by `_detect_presentation_region`.
_CLOTHING_REGION_FOR_PRESENTATION: dict[str, str] = {
    # feet
    "sock": "feet", "socks": "feet", "stocking": "feet", "stockings": "feet",
    "boot": "feet", "boots": "feet", "shoe": "feet", "shoes": "feet",
    "sandal": "feet", "sandals": "feet", "heel": "feet", "heels": "feet",
    "slipper": "feet", "slippers": "feet",
    # torso
    "bikini": "torso", "shirt": "torso", "blouse": "torso",
    "dress": "torso", "leotard": "torso", "swimsuit": "torso",
    # legs
    "pantyhose": "legs", "tights": "legs", "leggings": "legs",
    "thighhighs": "legs",
    # hands
    "glove": "hands", "gloves": "hands",
}

# Body-part word → region (direct mention). When user says "feet" /
# "toes" / "soles" / etc., the body region is named directly.
_BODY_PART_REGION_FOR_PRESENTATION: dict[str, str] = {
    "foot": "feet", "feet": "feet", "toe": "feet", "toes": "feet",
    "ankle": "feet", "ankles": "feet", "sole": "feet", "soles": "feet",
    "arm": "arms", "arms": "arms", "elbow": "arms", "elbows": "arms",
    "leg": "legs", "legs": "legs", "thigh": "legs", "thighs": "legs",
    "knee": "legs", "knees": "legs",
    "hand": "hands", "hands": "hands", "finger": "hands", "fingers": "hands",
    "chest": "torso", "breast": "torso", "breasts": "torso",
    "stomach": "torso", "belly": "torso",
}


def _detect_presentation_region(user_request: str) -> Optional[str]:
    """Detect a body region the user is implying as the subject of a
    presentation pose. Returns the region name ("feet" / "legs" /
    "hands" / etc.) or None.

    Two-signal detection: BOTH a clothing/body-part keyword AND a
    presentation cue must fire. Without the cue, "blue socks" is just
    outfit content. Without the keyword, "at viewer" is just camera
    framing. Together: "blue socks at viewer" → user wants feet
    presented to viewer.

    Deterministic and grounded in the user's curated taxonomy
    (clothing keywords, body-part keywords) — no LLM, no chip-specific
    hints. Used by the natlang pipeline as a high-confidence override
    when the LLM picker would otherwise vary."""
    if not user_request:
        return None
    if not _PRESENTATION_CUE_RE.search(user_request):
        return None
    lower = user_request.lower()
    # Tally regions referenced by clothing keywords first (clothing is
    # the strongest signal — `red socks at viewer` is unambiguous).
    region_votes: dict[str, int] = {}
    for word, region in _CLOTHING_REGION_FOR_PRESENTATION.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lower):
            region_votes[region] = region_votes.get(region, 0) + 2
    for word, region in _BODY_PART_REGION_FOR_PRESENTATION.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lower):
            region_votes[region] = region_votes.get(region, 0) + 1
    if not region_votes:
        return None
    return max(region_votes.items(), key=lambda kv: kv[1])[0]


# Map presentation region → slot_modifier canonical_tag. Mirrors the
# user's curated slot_modifier definitions: `presenting_foot` is the
# modifier for the "feet" region, etc. When a region is detected,
# the corresponding modifier (and its clears_slots) is implied.
_REGION_TO_POSE_MODIFIER: dict[str, str] = {
    "feet": "presenting_foot",
}


# "Wearing only X" / "just X" / "in nothing but X" / "with only X" —
# the user wants X to be the ENTIRE outfit. Strip everything else.
# The captured group is the X phrase, which gets slot-resolved by
# the caller (`_resolve_slot_from_phrase` / `_extract_color_item_for_slot`).
_STRIP_INTENT_RE = re.compile(
    r"\b(?:"
    r"wearing\s+only|"
    r"in\s+only|"
    r"only\s+wearing|"
    r"with\s+only|"
    r"in\s+nothing\s+but|"
    r"in\s+just|"
    r"just\s+wearing|"
    r"wearing\s+just|"
    r"only\s+(?:her|his|their|the)\s+|"
    r"just\s+(?:her|his|their|the)\s+"
    r")\s*(?:her|his|their|the|a|an)?\s*([^.,;\n]+?)(?:[.,;\n]|$)",
    re.IGNORECASE,
)


def _lookup_slot_modifier_by_canonical(canonical: str) -> Optional[dict]:
    """Find a slot_modifiers row by its canonical_tag. Used by the
    decompose-action dispatcher to resolve `outfit-modifier: barefoot`
    into the modifier's clears_slots + substitute_section without
    re-implementing the alias scan."""
    if not canonical:
        return None
    try:
        from .ai_api import _load_slot_modifiers
    except Exception:
        return None
    canon_lc = canonical.strip().lower().replace(" ", "_")
    for mod in _load_slot_modifiers():
        if (mod.get("canonical_tag") or "").strip().lower() == canon_lc:
            return mod
    return None


def extract_outfit_swap_from_text(text: str, bios: list[dict] | None) -> Optional["SwapOutfitDelta"]:
    """Full-request outfit-swap extractor — finds an "in X outfit" /
    "wearing X outfit" / "switch to X" phrasing in user_request and
    emits a SwapOutfitDelta ONLY when X matches a known bio outfit
    name. Mirrors decompose's `outfit:` sub-intent handler but runs
    on the raw user_request as a backstop.

    Conservatism: unlike decompose's path (which trusts the model's
    intent classification and emits a swap for unknown names so
    render-time DB re-pick can resolve them), this full-request
    extractor refuses unknown names. The catch-all patterns
    (`change to X`, `switch to X` with no `outfit` keyword) match too
    eagerly — `change to barefoot` would emit a swap to outfit
    "barefoot", clobbering legitimate state. When decompose handled
    the intent correctly, the backstop's `already_swapped` guard
    skips it; when decompose missed, we only act on high-confidence
    matches against curated outfit names."""
    if not text or not bios:
        return None
    from .ai_request_parser import _collect_outfit_names, _find_outfit_swap_with_span
    outfit_lookup = _collect_outfit_names(bios)
    if not outfit_lookup:
        return None
    result = _find_outfit_swap_with_span(text, outfit_lookup)
    if result is None:
        return None
    intent, _span = result
    if (intent.get("kind") or "") != "outfit_swap":
        return None
    # Require a positive bio-outfit match. outfit_id is set when the
    # name was found in `outfit_lookup`; absent when the catch-all
    # pattern fired with an unknown word.
    if intent.get("outfit_id") is None:
        return None
    outfit_name = (intent.get("outfit_name") or "").strip()
    if not outfit_name:
        return None
    char_tag = (intent.get("character_tag") or "").strip()
    source_display = _bio_display_for_tag(char_tag, bios) if char_tag else ""
    return SwapOutfitDelta(
        outfit_name=outfit_name,
        outfit_id=intent.get("outfit_id"),
        bio_slots=list(intent.get("slots") or []),
        bio_natlang=intent.get("natlang") or "",
        source_character_display=source_display,
    )


def extract_strip_intents_from_text(text: str) -> list[Delta]:
    """Full-request strip-intent extractor — symmetric to
    `extract_slot_fills_from_text`. Scans user_request for "wearing
    only X" / "just X" / "in nothing but X" patterns. Returns a list
    of [StripDelta, FillSlotDelta] pairs for each matched intent.

    Strip semantics: keep ONLY the slot the named X resolves to;
    everything else clears. The fill emits the resolved (color, item)
    in that slot. Mirrors the decompose-side `strip:` handler so
    behaviour is identical regardless of which path catches the intent.

    Returns deltas in apply-order (StripDelta before FillSlotDelta),
    ready to merge with the rest of the turn's deltas. Empty list when
    no strip pattern matched."""
    if not text or not text.strip():
        return []
    out: list[Delta] = []
    seen_slots: set[str] = set()
    for m in _STRIP_INTENT_RE.finditer(text):
        phrase = (m.group(1) or "").strip()
        if not phrase:
            continue
        slot = _resolve_slot_from_phrase(phrase)
        if not slot or slot in seen_slots:
            continue
        color, item = _extract_color_item_for_slot(phrase, slot)
        if not item:
            color, item = _split_color_item(phrase)
        if not item:
            continue
        seen_slots.add(slot)
        out.append(StripDelta(kept_slots=[slot]))
        out.append(FillSlotDelta(
            slot=slot, item=item, color=color, origin=ORIGIN_USER,
        ))
    return out


# Negation cue immediately preceding a clothing keyword. Wider net than
# `_NEGATION_BEFORE_RE` (which has a $-anchor for the slot-fill skip
# check). For slot-CLEAR detection we want to catch the cue anywhere
# in a short window before the keyword. Pattern: cue + optional
# articles/possessives + (optional intermediate words: color, qualifier).
_SLOT_CLEAR_CUE_RE = re.compile(
    r"\b(?:"
    r"no|not|without|sans|"
    r"remove(?:s|d)?|drop(?:s|ped)?|stop(?:s|ped)?|"
    r"take(?:s)?\s+off|took\s+off|threw\s+off|"
    r"kicks?\s+off|kicked\s+off|"
    r"loses?|lost|lose\s+the|"
    r"get\s+rid\s+of|"
    r"isn't\s+wearing|are\s+not\s+wearing|aren't\s+wearing|"
    r"isnt\s+wearing|not\s+wearing|never\s+wearing|"
    r"none|skip|skip\s+the|"
    r"minus"
    r")\s+"
    r"(?:the\s+|her\s+|his\s+|their\s+|a\s+|an\s+|any\s+|some\s+|"
    r"more\s+|all\s+|the\s+(?:the\s+)?)*"
    r"(?:\w+\s+){0,3}$",
    re.IGNORECASE,
)


def extract_slot_clears_from_text(text: str) -> list["ClearSlotDelta"]:
    """Full-request slot-clear extractor — symmetric to
    `extract_slot_fills_from_text` but inverted. Scans user_request for
    "no socks" / "remove the boots" / "without gloves" / "drop the
    leotard" / "take off her shoes" patterns. Each detected slot
    emits a ClearSlotDelta.

    Per-slot granularity — `no socks` clears ONLY legwear (not also
    footwear). For broader intents like `barefoot` use slot_modifier
    aliases that map to multiple slots.

    Negation cues covered: no, not, without, sans, remove(s/d),
    drop(s/ped), stop(s/ped), take(s) off, took off, threw off,
    kicks/kicked off, lose(s), lost, get rid of, isn't wearing,
    not wearing, never wearing, none, skip, minus.

    Dedup by slot — first occurrence wins."""
    if not text or not text.strip():
        return []
    lower = text.lower()
    out: list[ClearSlotDelta] = []
    seen_slots: set[str] = set()
    keyword_matches: list[tuple[int, int, str, str]] = []
    for kw, slot in _SLOT_KEYWORDS.items():
        kw_text = kw.replace("_", " ")
        for m in re.finditer(rf"(?<!\w){re.escape(kw_text)}(?!\w)", lower):
            keyword_matches.append((m.start(), m.end(), kw_text, slot))
    # Multi-word keywords first so e.g. "no garrison cap" doesn't get
    # claimed as "cap" alone.
    keyword_matches.sort(key=lambda t: (-(t[1] - t[0]), t[0]))
    for start, _end, _kw_text, slot in keyword_matches:
        if slot in seen_slots:
            continue
        window_before = lower[max(0, start - 50):start]
        if _SLOT_CLEAR_CUE_RE.search(window_before):
            out.append(ClearSlotDelta(slot=slot))
            seen_slots.add(slot)
    return out


def extract_slot_fills_from_text(text: str) -> list["FillSlotDelta"]:
    """Full-request slot-fill extractor — the symmetric backstop to
    `_detect_modifiers_in_text` from ai_api. Scans user_request for any
    clothing keyword from `_SLOT_KEYWORDS`, extracts surrounding
    color/qualifier prefix, and returns one FillSlotDelta per slot.

    Runs INDEPENDENT of decompose's section routing — the whole point.
    When 8B decompose lumps "blue socks" inside a `pose to ...` framing
    or otherwise mis-routes clothing into a non-outfit sub-intent, the
    fill is lost. This extractor recovers it from the raw user_request.

    Negation-aware: `no socks`, `without socks`, `remove socks`,
    `kicks off her boots`, `not wearing gloves` all skip — the user
    means the item is absent.

    Dedup by slot — first occurrence wins. The caller is expected to
    skip emitting for slots already filled by user-origin in state."""
    if not text or not text.strip():
        return []
    lower = text.lower()
    out: list[FillSlotDelta] = []
    seen_slots: set[str] = set()
    # Walk single-token keywords first (covers `socks`, `boots`, `cap`).
    # Multi-token keywords (`garrison cap`, `crop top`) are tried in a
    # second pass — the single-token version may have already claimed
    # the slot, in which case we keep the richer multi-word phrase.
    keyword_matches: list[tuple[int, int, str, str]] = []  # (start, end, keyword, slot)
    for kw, slot in _SLOT_KEYWORDS.items():
        kw_text = kw.replace("_", " ")
        for m in re.finditer(rf"(?<!\w){re.escape(kw_text)}(?!\w)", lower):
            keyword_matches.append((m.start(), m.end(), kw_text, slot))
    # Sort longest-first so multi-word keywords win over their single-
    # word substrings (`garrison cap` → headwear before `cap` alone).
    keyword_matches.sort(key=lambda t: (-(t[1] - t[0]), t[0]))
    for start, end, kw_text, slot in keyword_matches:
        if slot in seen_slots:
            continue
        window_before = lower[max(0, start - 30):start]
        if _NEGATION_BEFORE_RE.search(window_before):
            continue
        # Color-only prefix capture. Walk backwards from the keyword
        # token-by-token; collect immediately-preceding _KNOWN_COLORS
        # tokens (consecutive). Stop at first non-color word — that's
        # the boundary between the "blue socks" phrase and the
        # surrounding context. Unlike _extract_color_item_for_slot
        # (which takes everything before the keyword as descriptor),
        # this stays tight to the clothing phrase and doesn't sweep
        # up sentence-level context like "pose to legs up showing".
        preceding_tokens = re.findall(r"\w+", window_before)
        color_tokens: list[str] = []
        for tok in reversed(preceding_tokens):
            if tok in _KNOWN_COLORS:
                color_tokens.insert(0, tok)
            else:
                break
        color = " ".join(color_tokens)
        item = kw_text.replace(" ", "_")
        out.append(FillSlotDelta(
            slot=slot, item=item, color=color, origin=ORIGIN_USER,
        ))
        seen_slots.add(slot)
    return out


# ── refresh state with current bios (Phase A plan: refresh bio fields on load) ──

def synthesize_node_prompt_from_ingest(ingested: list[dict],
                                        char_display: str = "",
                                        char_series: str = "",
                                        outfit_name: str = "") -> str:
    """Build a sectioned node_prompt string from ingested per-section
    bodies. Used after ingestion to give the renderer something to
    preserve verbatim — without this, flat user prose has no section
    boundaries and preservation can't preserve anything."""
    bodies: dict[str, str] = {}
    for fact in ingested or []:
        field = (fact.get("field") or "").lower()
        text = (fact.get("text") or "").strip()
        if not text or not field.endswith("_body"):
            continue
        kind = field[:-len("_body")]
        if kind not in bodies:
            bodies[kind] = text
    if not bodies:
        return ""
    parts: list[str] = []
    char_header = "// Character:"
    if char_display:
        suffix = char_display
        if char_series:
            suffix += f" ({char_series})"
        char_header = f"// Character: {suffix}"
    if "character" in bodies:
        parts.append(f"{char_header}\n{bodies['character']}")
    if "outfit" in bodies:
        outfit_header = "// Outfit:"
        if outfit_name:
            outfit_header = f"// Outfit: {outfit_name}"
            if char_display:
                outfit_header += f" from Character: {char_display}"
        parts.append(f"{outfit_header}\n{bodies['outfit']}")
    if "pose" in bodies:
        parts.append(f"// Pose:\n{bodies['pose']}")
    if "expression" in bodies:
        parts.append(f"// Expression:\n{bodies['expression']}")
    if "setting" in bodies:
        parts.append(f"// Scene:\n{bodies['setting']}")
    if "style" in bodies:
        parts.append(f"// Style:\n{bodies['style']}")
    return "\n\n".join(parts)


def seed_state_from_ingested_facts(state: PromptState,
                                    bios: list[dict] | None,
                                    ingested: list[dict]) -> None:
    """Apply LLM-extracted facts (from existing prose) to state. Mutates
    state in place. Each fact is one of:
      {field: 'character', text: 'cammy_white'}
      {field: 'outfit', text: 'blue leotard'}
      {field: 'modifier', text: 'barefoot'}
      {field: 'pose', text: 'sitting'}
      {field: 'expression', text: 'sultry'}
      {field: 'setting', text: 'dungeon'}
      {field: 'style', text: 'hyperrealistic anime'}

    Outfit items get resolved to slot via the same _resolve_slot_from_phrase
    used by FillSlotDelta. Items that don't resolve to a slot fall into
    user_extra_phrases.

    Modifiers get DB-resolved (clears_slots) via _detect_modifiers_in_text
    so they propagate clears correctly."""
    if not ingested:
        return
    char = state.primary_character()
    if char is None and bios:
        # Look for a character ingested fact, otherwise use first bio.
        char_text = next((f["text"] for f in ingested if f.get("field") == "character"), "")
        if char_text:
            for bio in bios:
                tag = (bio.get("tag") or "").lower()
                disp = (bio.get("display") or "").lower()
                if tag == char_text.lower() or disp == char_text.lower().replace("_", " "):
                    char = _character_from_bio(bio)
                    state.characters.append(char)
                    break
        if char is None:
            char = _character_from_bio(bios[0])
            state.characters.append(char)
    if char is None:
        return

    # Reset outfit slot_states to empty so we don't mix bio-defaults with
    # ingested facts. The user's prose IS the truth.
    char.outfit.slot_states = _empty_slot_states()
    char.outfit.active_modifiers = []
    char.outfit.user_extra_phrases = []
    char.pose.descriptive_facts = []
    char.pose.pose_modifiers = []

    # Modifier DB lookup so clears_slots propagate.
    try:
        from .ai_api import _detect_modifiers_in_text
    except Exception:
        _detect_modifiers_in_text = None

    for fact in ingested:
        field = (fact.get("field") or "").lower()
        text = (fact.get("text") or "").strip()
        if not text:
            continue
        if field == "outfit":
            slot = _resolve_slot_from_phrase(text)
            if slot:
                color, item = _extract_color_item_for_slot(text, slot)
                if not item:
                    color, item = _split_color_item(text)
                existing = char.outfit.slot_states[slot]
                if existing.state == SLOT_STATE_FILLED and existing.item == item:
                    # Same item appears multiple times in prose with different
                    # descriptors (e.g. `blue leotard` then `Highleg leotard`).
                    # Merge the descriptors so neither qualifier is lost.
                    color = _merge_descriptor_tokens(existing.color, color)
                char.outfit.slot_states[slot] = SlotState(
                    state=SLOT_STATE_FILLED,
                    item=item,
                    color=color,
                    origin=ORIGIN_USER,
                )
            else:
                char.outfit.user_extra_phrases.append(text)
        elif field == "modifier":
            mods = []
            if _detect_modifiers_in_text:
                try:
                    mods = _detect_modifiers_in_text(text) or []
                except Exception:
                    mods = []
            if mods:
                for m in mods:
                    canonical = (m.get("canonical_tag") or "").lower()
                    section = (m.get("substitute_section") or "").strip().lower()
                    clears = list(m.get("clears_slots") or [])
                    if section == "pose":
                        if canonical and canonical not in char.pose.pose_modifiers:
                            char.pose.pose_modifiers.append(canonical)
                    else:
                        if canonical and canonical not in char.outfit.active_modifiers:
                            char.outfit.active_modifiers.append(canonical)
                        for slot_name in clears:
                            slot_name = (slot_name or "").strip().lower()
                            if slot_name in SLOT_NAMES:
                                char.outfit.slot_states[slot_name] = SlotState(
                                    state=SLOT_STATE_CLEARED,
                                    by_modifier=canonical,
                                )
            else:
                # No DB lookup or no match — store as a raw active modifier.
                canon = text.lower().replace(" ", "_")
                if canon not in char.outfit.active_modifiers:
                    char.outfit.active_modifiers.append(canon)
        elif field == "pose":
            if text not in char.pose.descriptive_facts:
                char.pose.descriptive_facts.append(text)
        elif field == "expression":
            state.expression = text if not state.expression else state.expression
            # Don't overwrite if multiple expression lines arrive — first wins.
        elif field == "setting":
            if not state.setting:
                state.setting = text
        elif field == "style":
            if state.style is None:
                state.style = StyleState(template_id="", name=text)


def changed_sections_from_deltas(deltas: list) -> set[str]:
    """Map each delta type to the section it touches. Returns a set of
    section kinds ('character', 'outfit', 'pose', 'expression',
    'setting', 'style') that need re-rendering this turn. Sections NOT
    in the result are preserved verbatim from node_prompt."""
    out: set[str] = set()
    for d in deltas:
        if isinstance(d, SwapCharacterDelta):
            out.add("character")
            # SwapCharacter resets outfit/pose to new bio's defaults too
            out.add("outfit")
            out.add("pose")
        elif isinstance(d, (SwapOutfitDelta, StripDelta, FillSlotDelta,
                             ClearSlotDelta, ApplyModifierDelta)):
            out.add("outfit")
        elif isinstance(d, (SwapPoseDelta, ApplyPoseChipDelta,
                             PoseChangeDelta, ClearPoseDelta)):
            out.add("pose")
        elif isinstance(d, (SetExpressionDelta, ClearExpressionDelta)):
            out.add("expression")
        elif isinstance(d, (SetSettingDelta, ClearSettingDelta)):
            out.add("setting")
        elif isinstance(d, (SwapStyleDelta, ClearStyleDelta)):
            out.add("style")
    return out




def refresh_state_with_bios(state: PromptState,
                            bios: list[dict] | None) -> PromptState:
    """Build a fresh state from current bios and overlay the user mods
    from `state` on top.

    User mods preserved across refresh:
      - outfit.active_modifiers (user-applied modifier canonicals)
      - outfit.user_extra_phrases (user free-form items, FILTERED to drop
        any that overlap with bio's slot items — prevents v1-legacy state
        from echoing "Red socks" beside the new legwear slot fill)
      - outfit.slot_states[s] where origin == ORIGIN_USER
      - outfit.slot_states[s] where state == CLEARED and by_modifier set
        (modifier-driven clears stay; their attribution survives a
        refresh because the modifier itself is in active_modifiers)
      - pose.descriptive_facts, pose.pose_modifiers
      - pose.natlang_anchor cleared if anchor was overridden last turn
      - state.expression, state.setting, state.style

    Empty `state` → returns a fresh state from bios with no user mods.
    """
    fresh = PromptState()
    if not bios:
        return state
    for bio in bios:
        fresh.characters.append(_character_from_bio(bio))

    if not state.characters:
        # First turn: just bios, no user mods to overlay.
        fresh.expression = state.expression
        fresh.setting = state.setting
        fresh.style = state.style
        return fresh

    # Subject preservation across turns: when prior state already has
    # subjects, restrict fresh.characters to those tags. Other matched
    # bios (e.g. Chun-Li loaded for a cross-borrow outfit lookup in
    # "swap her outfit for chun-li's outfit") stay available via the
    # `bios` parameter for SwapOutfitDelta source resolution but don't
    # render as subject characters. Without this filter, the second
    # turn outputs both Cammy AND Chun-Li as separate Character/Outfit
    # blocks. The existing subject_tags pruning in parse_request_via_
    # decompose only fires when the user explicitly names the subject
    # in decompose; pronoun-based requests like "swap her outfit..."
    # leave subject_tags empty.
    state_tags = {c.tag for c in state.characters if c.tag}
    if state_tags:
        scoped = [c for c in fresh.characters if c.tag in state_tags]
        if scoped:
            fresh.characters = scoped

    # Pair fresh chars with old chars by tag (multi-character extension safe).
    old_by_tag = {c.tag: c for c in state.characters}
    for fresh_char in fresh.characters:
        old_char = old_by_tag.get(fresh_char.tag)
        if old_char is None:
            continue
        # Outfit user mods
        fresh_char.outfit.active_modifiers = list(old_char.outfit.active_modifiers)
        for slot_name, slot_state in old_char.outfit.slot_states.items():
            if slot_name not in SLOT_NAMES:
                continue
            if slot_state.origin == ORIGIN_USER and slot_state.state == SLOT_STATE_FILLED:
                fresh_char.outfit.slot_states[slot_name] = slot_state
            elif slot_state.state == SLOT_STATE_CLEARED and slot_state.by_modifier:
                fresh_char.outfit.slot_states[slot_name] = slot_state
        fresh_char.outfit.user_extra_phrases = _dedup_extras_against_slots(
            list(old_char.outfit.user_extra_phrases),
            fresh_char.outfit,
        )
        # Pose user mods
        fresh_char.pose.descriptive_facts = list(old_char.pose.descriptive_facts)
        fresh_char.pose.pose_modifiers = list(old_char.pose.pose_modifiers)
        # Chip-anchored pose (bio_pose_id=None + natlang_anchor=chip
        # natlang + name set) survives across iterations. Without this
        # preservation, an outfit-only edit ("wearing pink socks")
        # would silently drop the prior chip and fall back to whatever
        # the bio's matched_pose happens to be. The chip is user-
        # established state — must persist until explicitly changed.
        if (old_char.pose.bio_pose_id is None
                and old_char.pose.natlang_anchor.strip()
                and old_char.pose.name.strip()):
            fresh_char.pose.name = old_char.pose.name
            fresh_char.pose.bio_pose_id = None
            fresh_char.pose.natlang_anchor = old_char.pose.natlang_anchor
            fresh_char.pose.is_signature = old_char.pose.is_signature
            fresh_char.pose.source_display = old_char.pose.source_display
        # Anchor override: if old pose had no bio_pose_id but did have
        # natlang_anchor cleared, the user posture-overrode last turn.
        # Preserve that override on refresh.
        elif old_char.pose.bio_pose_id is None and old_char.pose.descriptive_facts:
            fresh_char.pose.bio_pose_id = None
            fresh_char.pose.natlang_anchor = ""

    fresh.expression = state.expression
    fresh.setting = state.setting
    fresh.style = state.style
    return fresh


def _dedup_extras_against_slots(extras: list[str],
                                outfit: OutfitState) -> list[str]:
    """Drop any user_extra_phrase whose tokens overlap with a currently
    filled slot's color+item phrase. Catches the v1-legacy case where
    extras carries "red socks" alongside slot_states[legwear]=red socks
    — the extras entry would otherwise duplicate as a trailing sentence.
    """
    if not extras:
        return []
    filled_phrases: set[str] = set()
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state == SLOT_STATE_FILLED and s.item:
            tokens = re.findall(r"\w+", _slot_phrase_for_dedup(s).lower())
            if tokens:
                filled_phrases.add(" ".join(tokens))
    out: list[str] = []
    for raw in extras:
        if not isinstance(raw, str) or not raw.strip():
            continue
        tokens = re.findall(r"\w+", raw.lower())
        candidate = " ".join(tokens)
        if not candidate:
            continue
        if candidate in filled_phrases:
            continue
        # Also drop substrings — "red socks" extras when slot has "red socks"
        if any(candidate in fp or fp in candidate for fp in filled_phrases):
            continue
        out.append(raw.strip())
    return out


def _slot_phrase_for_dedup(s: SlotState) -> str:
    parts = [s.color, s.item.replace("_", " ")]
    return " ".join(p for p in parts if p)


# ── applier: state + deltas → state ─────────────────────────────────

def apply_deltas(state: PromptState,
                 deltas: list[Delta],
                 bios: list[dict] | None) -> PromptState:
    """Walk deltas in given order, mutating state in place. Returns the
    same state for chaining. Modifier propagation runs after each slot
    fill so modifiers whose clears are all filled get dropped.

    bios is needed for SwapCharacterDelta (load new character's bio) and
    SwapOutfitDelta (resolve new outfit slots when outfit_id wasn't
    pre-resolved by the parser)."""
    bios = bios or []
    for delta in deltas:
        _apply_one(state, delta, bios)
    # Final propagation pass to handle any modifier whose clears were
    # all filled by user-supplied FillSlotDeltas.
    char = state.primary_character()
    if char:
        update_active_modifiers_from_slots(char.outfit)
    return state


def _apply_one(state: PromptState, delta: Delta, bios: list[dict]) -> None:
    if isinstance(delta, SwapCharacterDelta):
        _apply_swap_character(state, delta, bios)
        return
    # State-level deltas — no character context required. These have to
    # work for non-character prompts ("a table with fruit in a bowl")
    # where state.characters is empty. Run them BEFORE the char-None
    # gate so they apply even when no bio is loaded.
    if isinstance(delta, SetExpressionDelta):
        state.expression = delta.text
        return
    if isinstance(delta, SetSettingDelta):
        # Multiple `setting:` sub-intents from decompose accumulate as
        # a comma-joined phrase rather than last-wins, so a request
        # like "a table with fruit in a bowl" that decompose splits
        # into setting: table / setting: fruit / setting: bowl renders
        # as one coherent line instead of just "bowl".
        existing = (state.setting or "").strip()
        addition = (delta.text or "").strip()
        if not addition:
            return
        if existing and addition.lower() not in existing.lower():
            state.setting = f"{existing}, {addition}"
        else:
            state.setting = addition
        return
    if isinstance(delta, SwapStyleDelta):
        state.style = StyleState(template_id=delta.template_id, name=delta.name)
        return
    if isinstance(delta, ClearSettingDelta):
        state.setting = ""
        return
    if isinstance(delta, ClearExpressionDelta):
        state.expression = ""
        return
    if isinstance(delta, ClearStyleDelta):
        state.style = None
        return
    char = state.primary_character()
    if char is None:
        # Remaining deltas (outfit/pose/etc) need a character context.
        # Without one we can't apply them — silently drop.
        return
    if isinstance(delta, SwapOutfitDelta):
        _apply_swap_outfit(char, delta, bios)
    elif isinstance(delta, StripDelta):
        _apply_strip(char, delta)
    elif isinstance(delta, FillSlotDelta):
        _apply_fill_slot(char, delta)
        update_active_modifiers_from_slots(char.outfit)
    elif isinstance(delta, ClearSlotDelta):
        _apply_clear_slot(char, delta)
    elif isinstance(delta, ApplyModifierDelta):
        _apply_modifier(char, delta)
    elif isinstance(delta, SwapPoseDelta):
        _apply_swap_pose(char, delta)
    elif isinstance(delta, ApplyPoseChipDelta):
        _apply_pose_chip(char, delta)
    elif isinstance(delta, PoseChangeDelta):
        _apply_pose_change(char, delta)
    elif isinstance(delta, ClearPoseDelta):
        from .prompt_state import PoseState as _PoseState
        char.pose = _PoseState()


def _apply_swap_character(state: PromptState,
                          delta: SwapCharacterDelta,
                          bios: list[dict]) -> None:
    """Replace the entire character block. Loses Cammy-specific
    slot/modifier/pose state — that's the design (the new character has
    its own bio defaults)."""
    new_bio = next(
        (b for b in bios if (b.get("tag") or "").lower() == delta.target_tag.lower()),
        None,
    )
    if new_bio is None:
        # Bio wasn't loaded (parser fallback case). Build a stub character;
        # the render flow's bio retrieval will refresh on next call.
        char = CharacterState(tag=delta.target_tag, display=delta.target_display or delta.target_tag)
        if state.characters:
            state.characters[0] = char
        else:
            state.characters.append(char)
        return
    char = _character_from_bio(new_bio)
    if state.characters:
        state.characters[0] = char
    else:
        state.characters.append(char)


def _apply_swap_outfit(char: CharacterState, delta: SwapOutfitDelta, bios: list[dict]) -> None:
    """Replace outfit. slot_states reset from delta.bio_slots; if empty,
    leave slot_states empty (custom outfit). active_modifiers persist
    across the swap (a user-applied 'barefoot' carries to the new
    outfit).

    user_extra_phrases do NOT persist across SwapOutfitDelta: a
    complete outfit swap means the new bio's prose is authoritative,
    so prior-outfit extras don't follow. This prevents bio-derived
    descriptors that ingest mis-classified as user extras (e.g. Delta
    Red's 'upside-down red triangle insignia' getting carried into a
    cross-borrowed Chun-Li outfit) from defeating the bio-anchor
    short-circuit in render_outfit_section."""
    keep_modifiers = list(char.outfit.active_modifiers)
    keep_user_extras: list[str] = []
    new_slot_states = _empty_slot_states()
    for s in delta.bio_slots:
        if not isinstance(s, dict):
            continue
        slot_type = (s.get("slot_type") or s.get("slot") or "").strip().lower()
        if slot_type not in SLOT_NAMES:
            continue
        item = (s.get("item") or "").strip()
        if not item:
            continue
        new_slot_states[slot_type] = SlotState(
            state=SLOT_STATE_FILLED,
            item=item,
            color=(s.get("color") or "").strip(),
            origin=ORIGIN_BIO,
            bio_sentence=(s.get("bio_sentence") or "").strip(),
        )
    # Source attribution: blank when the borrowed outfit belongs to the
    # subject character (regular swap), populated when it came from a
    # different bio (cross-character borrow). Compare against subject
    # display because that's what _outfit_header would otherwise emit.
    source = (delta.source_character_display or "").strip()
    if source and char.display and source.lower() == char.display.lower():
        source = ""
    char.outfit = OutfitState(
        name=delta.outfit_name,
        bio_outfit_id=delta.outfit_id,
        natlang_anchor=delta.bio_natlang,
        slot_states=new_slot_states,
        active_modifiers=keep_modifiers,
        user_extra_phrases=keep_user_extras,
        source_display=source,
    )
    # Compatible modifier check — re-clear slots based on persisted modifiers.
    _re_apply_persisted_modifiers(char.outfit, bios)


def _apply_strip(char: CharacterState, delta: StripDelta) -> None:
    """Strip every slot not in kept_slots, regardless of origin. The
    user explicitly asked to strip — prior user-fills should NOT
    survive (otherwise `wearing only red socks` against a state with
    a user-filled leotard would keep the leotard).

    Compound `wearing only X` re-fills the kept slot via FillSlotDelta
    that runs AFTER strip in delta order, so the kept-slot fill is
    preserved by the strip's `kept_slots` skip + the subsequent fill."""
    kept = {s.lower() for s in delta.kept_slots}
    for n in SLOT_NAMES:
        if n in kept:
            continue
        char.outfit.slot_states[n] = SlotState(
            state=SLOT_STATE_CLEARED,
            by_modifier="strip",
        )


def _apply_fill_slot(char: CharacterState, delta: FillSlotDelta) -> None:
    if delta.slot not in SLOT_NAMES:
        return
    char.outfit.slot_states[delta.slot] = SlotState(
        state=SLOT_STATE_FILLED,
        item=delta.item,
        color=delta.color,
        origin=delta.origin or ORIGIN_USER,
    )


def _apply_clear_slot(char: CharacterState, delta: ClearSlotDelta) -> None:
    """Set the named slot to CLEARED state with user-explicit
    attribution. Preserves prior item/color so downstream prose
    stripping can locate the corresponding phrase in the outfit body
    and remove it (mirrors `_apply_modifier`'s preservation pattern)."""
    slot_name = (delta.slot or "").strip().lower()
    if slot_name not in SLOT_NAMES:
        return
    cur = char.outfit.slot_states.get(slot_name)
    prior_item = (cur.item if cur else "") or ""
    prior_color = (cur.color if cur else "") or ""
    char.outfit.slot_states[slot_name] = SlotState(
        state=SLOT_STATE_CLEARED,
        by_modifier="user_remove",
        item=prior_item,
        color=prior_color,
    )


def _apply_modifier(char: CharacterState, delta: ApplyModifierDelta) -> None:
    """Apply an outfit-domain (or pose-domain) modifier. The user typed
    this modifier THIS turn, so it overrides prior user fills in the
    slots it clears — typing `make barefoot` while legwear has a
    user-filled `blue socks` should clear legwear and apply barefoot,
    not silently no-op because the prior socks 'win'.

    The reverse direction (user fills a slot AFTER modifier was active
    in prior state) is handled by update_active_modifiers_from_slots
    after FillSlotDeltas apply — that's where contradicted_by_user_fill
    drops the modifier."""
    canonical = delta.canonical
    if not canonical:
        return
    if delta.substitute_section == "pose":
        if canonical not in char.pose.pose_modifiers:
            char.pose.pose_modifiers.append(canonical)
        return
    if canonical not in char.outfit.active_modifiers:
        char.outfit.active_modifiers.append(canonical)
    for slot_name in delta.clears_slots:
        slot_name = (slot_name or "").strip().lower()
        if slot_name not in SLOT_NAMES:
            continue
        prior = char.outfit.slot_states.get(slot_name)
        # Preserve prior item/color on the cleared SlotState so render
        # post-passes can locate the corresponding phrase in the anchor
        # prose and remove it. Without this the item info is lost the
        # moment a modifier fires, leaving "knee-high brown leather
        # boots" stuck in the body when footwear is supposed to be gone.
        # Also preserve when the second modifier hits an already-cleared
        # slot that had item info from the first modifier (e.g. both
        # barefoot AND presenting_foot clear footwear — second clear
        # mustn't wipe the boots item info that barefoot saved).
        if prior and (prior.state == SLOT_STATE_FILLED or prior.item):
            prior_item = prior.item
            prior_color = prior.color
        else:
            prior_item = ""
            prior_color = ""
        char.outfit.slot_states[slot_name] = SlotState(
            state=SLOT_STATE_CLEARED,
            by_modifier=canonical,
            item=prior_item,
            color=prior_color,
        )


def _apply_swap_pose(char: CharacterState, delta: SwapPoseDelta) -> None:
    """Replace pose identity with a borrowed bio's data (typically a
    cross-character borrow). Wipes any prior descriptive_facts and
    pose_modifiers — the new bio anchor is the truth."""
    source = (delta.source_character_display or "").strip()
    if source and char.display and source.lower() == char.display.lower():
        source = ""
    char.pose = PoseState(
        name=delta.pose_name,
        bio_pose_id=delta.pose_id,
        natlang_anchor=delta.bio_natlang,
        is_signature=delta.is_signature,
        descriptive_facts=[],
        pose_modifiers=[],
        source_display=source,
    )


def _apply_pose_chip(char: CharacterState, delta: ApplyPoseChipDelta) -> None:
    """Replace pose identity with a generic chip from pose_items / etc.
    Wipes descriptive_facts and pose_modifiers — the chip's authored
    natlang IS the pose. bio_pose_id stays None so the header renders
    `// Pose: <Display Name>` without character attribution (the chip
    is character-agnostic, unlike SwapPoseDelta's bio borrow)."""
    from .prompt_state import PoseState
    char.pose = PoseState(
        name=delta.display_name or delta.chip_tag.replace("_", " ").title(),
        bio_pose_id=None,
        natlang_anchor=delta.base_natlang,
        is_signature=False,
        descriptive_facts=[],
        pose_modifiers=[],
        source_display="",
    )


def _apply_pose_change(char: CharacterState, delta: PoseChangeDelta) -> None:
    """Drop any descriptive_facts in `replaces`, append `adds`. If
    is_anchor_override, also clear the bio_pose_id so the renderer knows
    to compose pose prose from facts only (not the bio anchor).

    replaces_all wipes the entire descriptive_facts list before adding —
    used when this turn introduces fresh pose-relevant text and prior-
    turn carry-over should be discarded. Without this, multi-turn
    iteration accumulates every pose phrase the user has ever typed."""
    if delta.replaces_all:
        char.pose.descriptive_facts = []
    elif delta.replaces:
        replace_set = {s.lower() for s in delta.replaces}
        char.pose.descriptive_facts = [
            f for f in char.pose.descriptive_facts
            if f.lower() not in replace_set
        ]
    for add in delta.adds:
        if add and add not in char.pose.descriptive_facts:
            char.pose.descriptive_facts.append(add)
    if delta.is_anchor_override:
        char.pose.bio_pose_id = None
        char.pose.natlang_anchor = ""


def update_active_modifiers_from_slots(outfit: OutfitState) -> None:
    """Drop a modifier when ANY of its previously-cleared slots is now
    user-filled. Reasoning: when the user explicitly fills a slot the
    modifier had cleared (e.g. types `red socks` after `barefoot` was
    active), they're contradicting the modifier — the modifier is no
    longer their intent. Without this, "change to red socks" leaves
    "Barefoot." in the output beside the new socks fill.

    Also drops modifiers whose clears are now all out (state changed
    via SwapOutfit etc.) — those have no remaining attribution to
    survive on."""
    if not outfit.active_modifiers:
        return
    survivors: list[str] = []
    for mod in outfit.active_modifiers:
        # Find slots that THIS modifier had attributed-cleared OR that
        # were originally on its clears-list (need to re-derive: any
        # slot we look at could have been cleared by mod earlier).
        # Heuristic: a slot is "associated with mod" if currently
        # by_modifier == mod, OR if the slot is now USER-filled and the
        # mod's known clears include this slot.
        cleared_for_mod = [
            n for n in SLOT_NAMES
            if outfit.slot_states[n].state == SLOT_STATE_CLEARED
            and outfit.slot_states[n].by_modifier == mod
        ]
        # User-fill contradiction check — query the modifier DB to know
        # which slots this modifier originally clears. If ANY of those
        # is now user-filled, the user contradicted the modifier; drop it.
        contradicted_by_user = _modifier_contradicted_by_user_fill(mod, outfit)
        if cleared_for_mod and not contradicted_by_user:
            survivors.append(mod)
    outfit.active_modifiers = survivors


def _modifier_contradicted_by_user_fill(canonical: str, outfit: OutfitState) -> bool:
    """True when ANY slot in the modifier's canonical clears-list is now
    USER-filled. Uses the runtime modifier DB lookup; returns False if
    DB unavailable (defensive — without DB we can't tell, so don't drop)."""
    clears_by_canon = _load_modifier_clears([canonical])
    clears = clears_by_canon.get(canonical) or []
    for slot_name in clears:
        slot_name = slot_name.strip().lower()
        if slot_name not in SLOT_NAMES:
            continue
        s = outfit.slot_states[slot_name]
        if s.state == SLOT_STATE_FILLED and s.origin == ORIGIN_USER:
            return True
    return False


def _re_apply_persisted_modifiers(outfit: OutfitState, bios: list[dict]) -> None:
    """When SwapOutfit replaces slot_states, persisted modifiers need to
    re-clear the slots they apply to (against the new outfit's slot set).
    Fetches clears_slots via the runtime modifier DB lookup if available;
    silently drops if no DB."""
    if not outfit.active_modifiers:
        return
    clears_by_canon = _load_modifier_clears(outfit.active_modifiers)
    new_active: list[str] = []
    for mod in outfit.active_modifiers:
        clears = clears_by_canon.get(mod) or []
        cleared_any = False
        for slot_name in clears:
            slot_name = slot_name.strip().lower()
            if slot_name not in SLOT_NAMES:
                continue
            cur = outfit.slot_states[slot_name]
            if cur.state == SLOT_STATE_FILLED and cur.origin == ORIGIN_USER:
                continue
            outfit.slot_states[slot_name] = SlotState(
                state=SLOT_STATE_CLEARED,
                by_modifier=mod,
            )
            cleared_any = True
        if cleared_any:
            new_active.append(mod)
    outfit.active_modifiers = new_active


def _load_modifier_clears(canonicals: list[str]) -> dict[str, list[str]]:
    """Fetch each canonical's clears_slots from the runtime modifier DB.
    Returns {} if DB isn't available (caller silently skips re-application)."""
    try:
        from .ai_api import _load_slot_modifiers  # type: ignore
    except Exception:
        return {}
    try:
        all_mods = _load_slot_modifiers() or []
    except Exception:
        return {}
    by_canon = {(m.get("canonical_tag") or "").lower(): list(m.get("clears_slots") or [])
                for m in all_mods}
    return {c: by_canon.get(c, []) for c in canonicals}


# ── helper: build a CharacterState from a bio dict ─────────────────

def _character_from_bio(bio: dict) -> CharacterState:
    char = CharacterState(
        tag=bio.get("tag", ""),
        display=(bio.get("display") or "").strip() or bio.get("tag", ""),
        series=(bio.get("series") or "").strip(),
        base_natlang=(bio.get("base_natlang") or "").strip(),
        base_tags=(bio.get("base_tags") or "").strip(),
    )
    outfit_d = bio.get("user_requested_outfit") or bio.get("default_outfit")
    if outfit_d:
        slot_states = _empty_slot_states()
        for s in (outfit_d.get("slots") or []):
            if not isinstance(s, dict):
                continue
            slot_type = (s.get("slot_type") or s.get("slot") or "").strip().lower()
            if slot_type not in SLOT_NAMES:
                continue
            item = (s.get("item") or "").strip()
            if not item:
                continue
            slot_states[slot_type] = SlotState(
                state=SLOT_STATE_FILLED,
                item=item,
                color=(s.get("color") or "").strip(),
                origin=ORIGIN_BIO,
                bio_sentence=(s.get("bio_sentence") or "").strip(),
            )
        char.outfit = OutfitState(
            name=(outfit_d.get("name") or "").strip(),
            bio_outfit_id=outfit_d.get("id"),
            natlang_anchor=(outfit_d.get("natlang") or "").strip(),
            slot_states=slot_states,
        )
    pose_d = bio.get("matched_pose")
    if pose_d:
        char.pose = PoseState(
            name=(pose_d.get("name") or "").strip(),
            bio_pose_id=pose_d.get("id"),
            natlang_anchor=(pose_d.get("natlang") or "").strip(),
            is_signature=bool(pose_d.get("is_signature")),
        )
    return char
