"""Section renderers for the natlang v2 pipeline (Phase D).

Each renderer produces ONE section's prose from PromptState. The pipeline
runs them after Phase C's apply_deltas has produced the final cumulative
state, so the renderers see a clean structured fact set with no need to
reconcile prior prose.

Renderers:
  - render_character_section(state)        — server, deterministic
  - render_outfit_section(state)           — server, slot-by-slot compose
  - render_pose_section(state, *, model)   — model call (async)
  - render_expression_section(state)       — server, just state.expression
  - render_setting_section(state)          — server, just state.setting

A facts-hash cache keeps byte-identity for unchanged sections across
turns (the LLM is stochastic; re-rendering with identical facts would
return slightly different prose otherwise). Cache is process-local.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Awaitable, Callable, Optional

_NL = chr(10)

from .prompt_state import (
    PromptState, OutfitState, PoseState, CharacterState, SlotState,
    SLOT_NAMES, SLOT_STATE_FILLED, SLOT_STATE_CLEARED,
    ORIGIN_BIO, ORIGIN_USER,
)


# ── caches ──────────────────────────────────────────────────────────

# {facts_hash: rendered_prose} — section-level prose cache. Keeps byte-
# identical output when the same facts hit the same renderer across turns.
_PROSE_CACHE: dict[str, str] = {}


def _facts_hash(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def clear_cache() -> None:
    """Test hook — drop the prose cache."""
    _PROSE_CACHE.clear()


# ── character section (server, deterministic) ─────────────────────

def render_character_section(state: PromptState,
                              char: Optional[CharacterState] = None) -> str:
    """Emit `// Character: <Name> (<Series>)` followed by bio.base_natlang
    verbatim. The metadata in the header lets callers (style-template
    matching, downstream search) see who the section is about without
    parsing the prose.

    `char` defaults to the primary; pass an explicit CharacterState to
    render any character in `state.characters` (multi-character mode)."""
    if char is None:
        char = state.primary_character()
    if char is None or not char.base_natlang.strip():
        return ""
    body = _strip_leading_section_header(char.base_natlang.strip())
    header_parts = ["// Character:"]
    name = (char.display or char.tag or "").strip()
    if name:
        header_parts.append(name)
        if char.series.strip():
            header_parts[-1] += f" ({char.series.strip()})"
    header = " ".join(header_parts)
    return f"{header}\n{body}"


# ── outfit section (server, slot-by-slot) ─────────────────────────

_FRUIT_OUTFIT_SYSTEM_PROMPT = (
    "You rewrite a single `// Outfit:` section to reflect updated "
    "structured facts. Preserve the user's exact phrasing (INCLUDING "
    "typos and capitalization) for items still present; change only "
    "what the facts say has changed.\n"
    "\n"
    "STRUCTURED FACTS ARE EXHAUSTIVE — anything not listed is GONE.\n"
    "\n"
    "Rules:\n"
    "- Output ONLY the rewritten outfit prose body — no header, no "
    "preamble, no quotes.\n"
    "- Items in the facts AND in existing prose: keep the existing "
    "prose's phrasing VERBATIM — every qualifier, fabric, cut, fit, "
    "color, descriptor, even typos. If existing says `light blue "
    "sleeveless thong leotard with stiff tight mock-neck`, output "
    "EXACTLY that, not `blue leotard`. The facts are a structural "
    "check (what's still present); the existing prose is the "
    "authoritative phrasing. NEVER compress to a bare item word.\n"
    "- Items in existing prose NOT in the facts: REMOVE them entirely.\n"
    "- Items in the facts NOT in existing prose: ADD them by "
    "EXTENDING the existing comma-list, not by appending standalone "
    "sentences.\n"
    "    * If existing reads `Wearing X, Y, Z.` and you must add W, "
    "the output is `Wearing X, Y, Z, and W.` — comma before the new "
    "item, conjunction `and` only before the final item.\n"
    "    * If you must add TWO new items W and V, the output is "
    "`Wearing X, Y, Z, W, and V.` — both go inside the same list.\n"
    "    * NEVER write `Wearing X, Y, Z. W. V.` — orphan period-"
    "sentences are wrong; new items belong INSIDE the existing "
    "comma-list with the qualifiers (color, fabric, descriptors) "
    "from the facts attached to each item.\n"
    "    * Preserve qualifiers from the facts verbatim. Fact item "
    "`black combat boots` → output `black combat boots` (NOT just "
    "`boots`).\n"
    "- The `modifier:` lines in the facts are the COMPLETE active list. "
    "ANY modifier sentence in existing prose (e.g. `Barefoot.`, "
    "`Topless.`) that is NOT in the facts modifier list MUST be "
    "removed entirely. If the facts have ZERO modifier lines, the "
    "output must have ZERO modifier sentences — drop them.\n"
    "- Modifiers IN the facts: emit each as a brief capitalized "
    "sentence at the end. Example: `Barefoot.`\n"
    "- All slots cleared + modifier present: output ONLY the modifier "
    "sentence(s).\n"
    "- All slots cleared + no modifiers: output `Otherwise nude.`\n"
    "- Do NOT invent details, add adjectives, or reword unchanged items.\n"
    "/no_think"
)


async def render_outfit_section(
    state: PromptState,
    existing_outfit_prose: str = "",
    *,
    model_compose: Optional[Callable[[str, str], Awaitable[str]]] = None,
    char: Optional[CharacterState] = None,
) -> str:
    """FRUIT-style outfit rendering: take existing prose + structured
    facts, ask the LLM to surgically update the prose preserving the
    user's phrasing where unchanged.

    `char` defaults to the primary; pass an explicit CharacterState to
    render any character's outfit in multi-character mode.

    Falls back to deterministic compose when model_compose is None or
    when there's no existing prose to preserve (build mode)."""
    if char is None:
        char = state.primary_character()
    if char is None:
        return ""
    outfit = char.outfit
    header = _outfit_header(char)

    # Bio-anchor short-circuit: outfit matches bio with at most a
    # modifier applied (barefoot, topless, etc.) -- emit bio prose
    # verbatim, then deterministically strip modifier-cleared items
    # and prepend the modifier substitute. Preserves the bio's rich
    # descriptive prose ("light blue sleeveless thong leotard with
    # stiff tight mock-neck, ribbed sweater fabric, ...") instead of
    # routing through FRUIT, which compresses to slot tokens
    # ("blue leotard, garrison cap, ...").
    #
    # Gate on natlang_anchor presence rather than bio_outfit_id: some
    # match-characters paths drop the id field but still populate the
    # anchor. Anchor only gets set from a real bio load, only cleared
    # when the user explicitly diverges from the bio outfit (fills /
    # strips / user_extras).
    bio_anchor_ok = (
        outfit.natlang_anchor.strip()
        and not outfit.user_extra_phrases
        and not _has_user_fills_or_strips(outfit)
    )
    if bio_anchor_ok:
        body = _strip_leading_section_header(outfit.natlang_anchor.strip())
        if outfit.active_modifiers:
            # Modifier present: drop items the modifier cleared, then
            # prepend the modifier's display token. Existing backstop
            # _drop_modifier_cleared_items handles the strip; we then
            # add modifier names as a short clause at the front so the
            # render makes the modifier explicit even if no body
            # sentence happened to mention it.
            body = _drop_modifier_cleared_items(body, outfit)
            mod_clause = ", ".join(
                m.replace("_", " ").capitalize()
                for m in outfit.active_modifiers
                if m
            )
            if mod_clause and mod_clause.lower() not in body.lower():
                body = f"{mod_clause}. {body}" if body else f"{mod_clause}."
        return f"{header}\n{body}" if body else ""

    # Build the structured facts block — what the model should reflect.
    facts = _outfit_facts_for_prompt(outfit)

    # When the user diverged from the bio outfit (fill/strip/modifier) and
    # we have no existing prose to anchor on, use the V2 chip-composed
    # natlang_anchor as the FRUIT seed. Without this, build-mode outfit
    # edits drop the rich curated prose ("green sleeveless thong leotard
    # with upside-down red triangle insignia...") in favor of generic
    # slot-summary text ("Wearing orange leotard, beret, ..."). The
    # anchor carries the user's authored chip-level detail; FRUIT just
    # has to swap the touched slot's phrasing.
    seed_prose = existing_outfit_prose.strip()
    if not seed_prose and outfit.natlang_anchor.strip():
        seed_prose = _strip_leading_section_header(outfit.natlang_anchor.strip())

    # No prose seed at all OR no model: deterministic compose fallback.
    if not seed_prose or model_compose is None:
        body = _compose_outfit_prose(outfit)
        return f"{header}\n{body}" if body else ""

    # FRUIT-style edit via LLM.
    cache_key = _facts_hash("outfit", facts, seed_prose)
    if cache_key in _PROSE_CACHE:
        return _PROSE_CACHE[cache_key]

    user_prompt = (
        f"Existing outfit prose:\n{seed_prose}\n\n"
        f"Structured facts (what the outfit should reflect now):\n{facts}\n\n"
        f"Rewrite the outfit prose to match the structured facts. "
        f"Preserve the user's phrasing for unchanged items."
    )
    body = ""
    try:
        raw = await model_compose(_FRUIT_OUTFIT_SYSTEM_PROMPT, user_prompt)
        if isinstance(raw, str):
            body = raw.strip()
    except Exception:
        body = ""
    if not body:
        body = _compose_outfit_prose(outfit)
    body = _strip_leading_section_header(body)
    if not body:
        return ""
    # User-fill backstop: 8B-class FRUIT calls occasionally drop a user
    # fill from the rewritten body (e.g. user asks for "red socks", facts
    # have legwear: red socks, LLM output omits it). Deterministically
    # inject any ORIGIN_USER slot phrase that isn't present.
    body = _inject_missing_user_fills(body, outfit)
    # Modifier-clear backstop: symmetric to the user-fill backstop. When
    # a modifier (barefoot, topless, etc.) clears a slot, the LLM should
    # remove the corresponding item from prose; at 8B it routinely keeps
    # the rich anchor sentence verbatim. Walk CLEARED slots that have
    # by_modifier set + prior item info preserved, and remove any phrase
    # in the body referencing that item.
    body = _drop_modifier_cleared_items(body, outfit)
    result = f"{header}\n{body}"
    _PROSE_CACHE[cache_key] = result
    return result


def _drop_modifier_cleared_items(body: str, outfit: OutfitState) -> str:
    """For each CLEARED-by-modifier slot with preserved item info, strip
    body phrases referencing that item.

    Phrase removal heuristic: split the body on commas and periods,
    drop any segment that contains the item word (e.g. "boots") OR
    the slot's source_phrase (e.g. "brown_boots"). Re-join survivors.
    Conservative — segments without the item survive, so an unrelated
    sentence mentioning a hand or face stays put."""
    targets: list[str] = []
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state != SLOT_STATE_CLEARED or not s.by_modifier:
            continue
        item_word = (s.item or "").replace("_", " ").strip().lower()
        if item_word:
            targets.append(item_word)
    if not targets:
        return body
    # Segment on commas/periods, keep delimiters so re-join preserves
    # punctuation rhythm.
    segments = re.split(r"([,.])", body)
    cleaned: list[str] = []
    skip_next_delim = False
    for seg in segments:
        if seg in (",", "."):
            if skip_next_delim:
                skip_next_delim = False
                continue
            cleaned.append(seg)
            continue
        seg_lc = seg.lower()
        if any(t in seg_lc for t in targets):
            skip_next_delim = True
            continue
        cleaned.append(seg)
    result = "".join(cleaned)
    # Tidy: leading commas and double-period from removed segments.
    result = re.sub(r"^[\s,]+", "", result).strip()
    result = re.sub(r",\s*\.", ".", result)
    result = re.sub(r"\.\s*\.", ".", result)
    result = re.sub(r",\s*,", ",", result)
    return result


def _inject_missing_user_fills(body: str, outfit: OutfitState) -> str:
    """For each filled slot, ensure its phrase appears in body. If not,
    inject it into the comma-list before the period.

    Catches two FRUIT failure modes at 8B:
    - ORIGIN_USER fill dropped (e.g. user said "red socks", LLM omits)
    - ORIGIN_BIO fill dropped when a modifier is active (e.g. barefoot
      causes LLM to over-collapse the body to "Barefoot." losing the
      other slots' items).
    """
    missing: list[str] = []
    body_lc = body.lower()
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state != SLOT_STATE_FILLED:
            continue
        phrase = _slot_phrase(s).strip()
        if not phrase:
            continue
        # Already present if any meaningful token appears (color or item).
        if phrase.lower() in body_lc:
            continue
        # Use the bare item as fallback check for cases like "red socks"
        # already in body as just "socks".
        item_lc = (s.item or "").replace("_", " ").lower().strip()
        if item_lc and item_lc in body_lc:
            continue
        missing.append(phrase)
    if not missing:
        return body
    # Inject before the final period of the first sentence (the
    # "Wearing ..." sentence). If no period found, append as a new
    # comma-extending tail.
    extra = ", " + ", and ".join(missing) if len(missing) == 1 else (
        ", " + ", ".join(missing[:-1]) + f", and {missing[-1]}"
    )
    # Find the first period that ends a sentence.
    m = re.search(r"\.(?:\s|$)", body)
    if m:
        insert_at = m.start()
        return body[:insert_at] + extra + body[insert_at:]
    return body + extra


def _outfit_facts_for_prompt(outfit: OutfitState) -> str:
    """Render slot_states + active_modifiers + user_extras as a flat
    list the LLM can match against existing prose. Section headings
    keep `item` (clothing) and `body_modifier` (barefoot/topless/etc.)
    distinct so the LLM doesn't conflate the design-quality `modifiers`
    slot (e.g. fingerless_gloves) with body-state modifiers."""
    item_lines: list[str] = []
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state == SLOT_STATE_FILLED and s.item:
            phrase = _slot_phrase(s)
            # Use "design quality" for the design-modifiers slot, "item"
            # for everything else, to dodge the modifier/modifiers
            # confusion in the LLM's output.
            label = "design quality" if n == "modifiers" else "item"
            item_lines.append(f"  - {label} ({n}): {phrase}")
    for extra in outfit.user_extra_phrases:
        if extra.strip():
            item_lines.append(f"  - extra item: {extra.strip()}")
    if not item_lines:
        item_lines.append("  (no clothing items — fully stripped)")

    if outfit.active_modifiers:
        mod_lines = [
            f"  - body_modifier: {m.replace('_', ' ')}"
            for m in outfit.active_modifiers
        ]
    else:
        mod_lines = [
            "  (NONE — drop all body-state modifier sentences "
            "like `Barefoot.` / `Topless.` from the existing prose)"
        ]

    return (
        "Items (clothing/accessories present — use these as the outfit description):\n"
        + "\n".join(item_lines)
        + "\n\nBody-state modifiers (active flags like barefoot/topless/nude):\n"
        + "\n".join(mod_lines)
    )


def render_outfit_section_legacy(state: PromptState) -> str:
    """Compose outfit prose from slot_states. Header includes outfit name
    and character so downstream consumers (style matching, patch-mode
    diffing) can see what's in this slot without parsing prose."""
    char = state.primary_character()
    if char is None:
        return ""
    outfit = char.outfit
    header = _outfit_header(char)

    if (
        outfit.natlang_anchor.strip()
        and not outfit.active_modifiers
        and not outfit.user_extra_phrases
        and not _has_user_fills_or_strips(outfit)
    ):
        body = _strip_leading_section_header(outfit.natlang_anchor.strip())
        return f"{header}\n{body}"

    body = _compose_outfit_prose(outfit)
    return f"{header}\n{body}" if body else ""


def _outfit_header(char: CharacterState) -> str:
    """Build `// Outfit: <Name> from Character: <SourceCharacter>`.

    When the outfit was borrowed from another character (e.g. user said
    `cammy white in chun-li's outfit`), `outfit.source_display` is set
    and the header attributes the outfit to the source bio (Chun-Li),
    not the subject (Cammy). Without this, readers see Chun-Li's qipao
    body labeled as if it belongs to Cammy."""
    parts = ["// Outfit:"]
    outfit_name = (char.outfit.name or "").strip()
    source = (char.outfit.source_display or "").strip()
    char_name = source or (char.display or char.tag or "").strip()
    if outfit_name:
        parts.append(outfit_name)
        if char_name:
            parts[-1] += f" from Character: {char_name}"
    return " ".join(parts)


def _has_user_fills_or_strips(outfit: OutfitState) -> bool:
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state == SLOT_STATE_FILLED and s.origin == ORIGIN_USER:
            return True
        if s.state == SLOT_STATE_CLEARED and s.by_modifier == "strip":
            return True
        # User-explicit single-slot clears ("no socks", "remove the
        # boots") must defeat the bio-anchor short-circuit — otherwise
        # the anchor renders verbatim and re-includes the item the
        # user just asked to remove.
        if s.state == SLOT_STATE_CLEARED and s.by_modifier == "user_remove":
            return True
    return False


def _compose_outfit_prose(outfit: OutfitState) -> str:
    """Walk slot_states deterministically. Filled-slot phrases render
    in slot taxonomy order so the output is stable across turns."""
    filled_phrases: list[str] = []
    seen_modifier_attribs: set[str] = set()

    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state == SLOT_STATE_FILLED and s.item:
            filled_phrases.append(_slot_phrase(s))

    # Active modifiers as a single trailing sentence ("Barefoot.")
    modifier_phrases: list[str] = []
    for mod in outfit.active_modifiers:
        if mod in seen_modifier_attribs:
            continue
        seen_modifier_attribs.add(mod)
        modifier_phrases.append(_modifier_to_prose(mod))

    pieces: list[str] = []
    if filled_phrases:
        pieces.append(_join_phrases_into_sentence(filled_phrases))
    if modifier_phrases:
        pieces.append(_join_modifier_sentences(modifier_phrases))

    # User extra phrases. Drop any that are clearly slot-taxonomy items
    # (they belong in slot_states, not extras) — without this, a stale
    # extras entry like "red socks" can echo as a trailing sentence even
    # after a barefoot modifier cleared the legwear slot. user_extras
    # should only carry items NOT in the slot taxonomy (rare free-form
    # additions like "tiara" if accessories isn't decomposed enough).
    filled_token_sets: list[set[str]] = []
    for n in SLOT_NAMES:
        s = outfit.slot_states[n]
        if s.state == SLOT_STATE_FILLED and s.item:
            tokens = set(re.findall(r"\w+", _slot_phrase(s).lower()))
            if tokens:
                filled_token_sets.append(tokens)
    for extra in outfit.user_extra_phrases:
        text = (extra or "").strip()
        if not text:
            continue
        extra_tokens = set(re.findall(r"\w+", text.lower()))
        # Drop if the phrase mentions ANY slot-taxonomy item (it should
        # have been a slot fill, not a free-form extra).
        if _phrase_is_slot_taxonomy(extra_tokens):
            continue
        if extra_tokens and any(extra_tokens.issubset(fs) or fs.issubset(extra_tokens)
                                for fs in filled_token_sets):
            continue
        if not text.endswith("."):
            text += "."
        pieces.append(text[0].upper() + text[1:])

    if not pieces:
        # Nothing filled, no modifiers — totally stripped.
        pieces.append("Otherwise nude.")

    return " ".join(pieces)


def _slot_phrase(s: SlotState) -> str:
    item = (s.item or "").replace("_", " ").strip()
    color = (s.color or "").strip()
    if color:
        return f"{color} {item}"
    return item


def _join_phrases_into_sentence(phrases: list[str]) -> str:
    """`["green leotard", "red gauntlets", "red socks"]`
       → "Wearing green leotard, red gauntlets, and red socks."
    """
    if not phrases:
        return ""
    if len(phrases) == 1:
        return f"Wearing {phrases[0]}."
    if len(phrases) == 2:
        return f"Wearing {phrases[0]} and {phrases[1]}."
    head = ", ".join(phrases[:-1])
    return f"Wearing {head}, and {phrases[-1]}."


def _modifier_to_prose(canonical: str) -> str:
    """`barefoot` → "Barefoot." `topless` → "Topless." Underscores
    become spaces, first letter capitalized."""
    text = canonical.replace("_", " ").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _join_modifier_sentences(modifier_phrases: list[str]) -> str:
    """Each modifier is a brief sentence ending in period."""
    return " ".join(p if p.endswith(".") else p + "." for p in modifier_phrases if p)


def _phrase_is_slot_taxonomy(tokens: set[str]) -> bool:
    """True when any token in `tokens` is a known clothing keyword from
    the slot taxonomy. user_extra_phrases that match this should be
    dropped — they belong in slot_states, not free-form extras."""
    if not tokens:
        return False
    try:
        from .natlang_facts import _SLOT_KEYWORDS as _SK
    except Exception:
        return False
    return any(t in _SK for t in tokens)


# ── pose section (model call with facts-hash cache) ──────────────

async def render_pose_section(state: PromptState,
                              *,
                              model_compose: Optional[Callable[[str, str], Awaitable[str]]] = None,
                              ) -> str:
    """Compose pose prose from bio anchor + descriptive_facts +
    pose_modifiers. Model call when descriptive_facts are user-supplied
    and would override/extend the anchor. Bio-anchor short-circuit when
    no user facts are present.

    model_compose: async callable (system_prompt, user_prompt) → str.
    Pass None to skip model entirely (returns deterministic fallback).
    """
    char = state.primary_character()
    if char is None:
        return ""
    pose = char.pose

    header = _pose_header(char)

    # Nothing-to-render guard: no bio anchor AND no user facts AND no
    # pose modifiers means the user didn't ask for a pose. Returning ""
    # so the orchestrator omits the section. Without this, the LLM
    # compose path below gets called with an empty user_prompt and
    # hallucinates a generic stock pose paragraph the user never asked
    # for — same class of leak as a stale expression carrying through.
    if (not pose.natlang_anchor.strip()
            and not pose.descriptive_facts
            and not pose.pose_modifiers):
        return ""

    # Bio short-circuit: no user facts to integrate.
    if not pose.descriptive_facts and not pose.pose_modifiers and pose.natlang_anchor.strip():
        body = _strip_leading_section_header(pose.natlang_anchor.strip())
        # Body-part-to-slot injection only applies to chip-anchored
        # poses (bio_pose_id=None). Bio-authored poses already account
        # for the character's outfit in their natlang and would
        # double-mention items if augmented.
        if pose.bio_pose_id is None:
            body = _inject_slot_context_into_pose_body(body, char)
        return f"{header}\n{body}"

    # Chip-anchored short-circuit with user facts: skip FRUIT compose.
    # The chip's authored natlang is precious — image models are tuned
    # for the specific phrasings the curator wrote, and FRUIT at 8B
    # paraphrases them into softer/less-specific text ("presenting
    # feet with focus on soles" → "feet resting near her torso, toes
    # pointed subtly upward"). Render as `<chip natlang>. <fact1>.
    # <fact2>.` — chip stays verbatim, user facts append cleanly. The
    # body-part-to-slot injection still applies for foot-region chips.
    if (pose.natlang_anchor.strip()
            and pose.bio_pose_id is None
            and (pose.descriptive_facts or pose.pose_modifiers)):
        anchor = _strip_leading_section_header(pose.natlang_anchor.strip())
        anchor = _inject_slot_context_into_pose_body(anchor, char)
        parts = [anchor.rstrip(".")] if anchor else []
        for fact in pose.descriptive_facts:
            fact_clean = (fact or "").strip().rstrip(".").strip()
            if fact_clean and fact_clean.lower() not in anchor.lower():
                parts.append(fact_clean[0].upper() + fact_clean[1:] if fact_clean else "")
        for mod in pose.pose_modifiers:
            mod_text = (mod or "").replace("_", " ").strip()
            if mod_text and mod_text.lower() not in anchor.lower():
                parts.append(mod_text[0].upper() + mod_text[1:])
        if parts:
            body = ". ".join(p for p in parts if p) + "."
            return f"{header}\n{body}"

    # Pure facts (no anchor, no model): compose deterministically.
    if not pose.natlang_anchor.strip() and not model_compose:
        body = _compose_pose_facts_only(pose)
        if not body:
            return ""
        return f"{header}\n{body}"

    # Cached?
    cache_key = _facts_hash(
        "pose",
        pose.natlang_anchor,
        tuple(pose.descriptive_facts),
        tuple(pose.pose_modifiers),
    )
    if cache_key in _PROSE_CACHE:
        return _PROSE_CACHE[cache_key]

    # Model call
    if model_compose is None:
        body = _compose_pose_facts_only(pose) or pose.natlang_anchor or ""
        if not body:
            return ""
        result = f"{header}\n{body}"
        _PROSE_CACHE[cache_key] = result
        return result

    system_prompt = _build_pose_system_prompt()
    user_prompt = _build_pose_user_prompt(pose)
    body = ""
    try:
        raw = await model_compose(system_prompt, user_prompt)
        if isinstance(raw, str):
            body = raw.strip()
    except Exception:
        body = ""
    # Fall back to deterministic compose on empty / non-string / exception.
    # Without this, a model timeout or connection-refused leaves the section
    # empty even when we have facts in hand.
    if not body:
        body = _compose_pose_facts_only(pose) or pose.natlang_anchor or ""
    body = _strip_leading_section_header(body)
    if not body:
        return ""
    # User-fact backstop: same shape as the outfit user-fill backstop.
    # When pose.descriptive_facts contains a user-named action ("sparring",
    # "kicking") the LLM compose at 8B occasionally paraphrases without
    # using the literal token, making intent untraceable downstream.
    # Inject any missing fact as a brief sentence so the user's wording
    # survives at minimum.
    body = _inject_missing_pose_facts(body, pose)
    result = f"{header}\n{body}"
    _PROSE_CACHE[cache_key] = result
    return result


# Body-part keyword → outfit slot names in priority order. Restricted
# to FOOT-REGION body parts only — image models default feet to bare
# when the pose body says "presenting feet" without mentioning footwear
# or socks, and this is the documented bug we're fixing. Other body
# parts (arms/hands/head/torso) don't have the same default-to-naked
# bias — a chip body saying "arms behind head" doesn't make the image
# model strip the top, so injecting "arms wearing leotard" adds noise
# without fixing anything (and actively produces gibberish for pose
# bodies that mention body parts incidentally, like "Both arms bent
# with hands behind head" → "Both arms wearing leotard bent with hands
# wearing gloves behind head wearing cap").
#
# Priority order matters for foot region: legwear (socks) takes
# precedence over footwear because socks are the closer-to-body layer
# and image models render socks when explicitly mentioned.
_POSE_BODY_PART_TO_SLOTS: dict[str, tuple[str, ...]] = {
    "feet": ("legwear", "footwear"),
    "foot": ("legwear", "footwear"),
    "soles": ("legwear", "footwear"),
    "sole": ("legwear", "footwear"),
    "toes": ("legwear", "footwear"),
    "toe": ("legwear", "footwear"),
}


def _inject_slot_context_into_pose_body(body: str, char: CharacterState) -> str:
    """Augment chip-anchored pose body with current slot content where
    a body-part keyword appears. The chip natlang is a generic template
    ("presenting feet with focus on soles"); the user's outfit slots
    carry the specifics (legwear=pink_socks). The image model sees the
    pose section in isolation from outfit and defaults body parts to
    bare unless the pose body explicitly names the clothing — so we
    inject the clothing reference at the body-part keyword position.

    Conservative: only injects when the slot is actually FILLED with a
    real item, and only once per slot per body. Skips when the chip
    body already mentions the slot's item (no double-mention). The
    chip's authored prose is preserved otherwise.

    Example: "sitting with legs up, presenting feet with focus on soles"
    + legwear=pink_socks → "sitting with legs up, presenting feet
    wearing pink socks with focus on soles". Bare-feet case
    (legwear+footwear both cleared) leaves the body untouched."""
    if not body or char is None:
        return body
    outfit = char.outfit
    body_lc = body.lower()
    out = body
    used_slots: set[str] = set()
    for body_part in _POSE_BODY_PART_TO_SLOTS:
        # Word-boundary search against the (possibly already-augmented)
        # body so re-runs over the same body part skip cleanly.
        m = re.search(rf"\b{re.escape(body_part)}\b", out, re.IGNORECASE)
        if not m:
            continue
        slots = _POSE_BODY_PART_TO_SLOTS[body_part]
        fill = None
        for slot_name in slots:
            if slot_name in used_slots:
                continue
            ss = outfit.slot_states.get(slot_name)
            if ss is None:
                continue
            if ss.state != SLOT_STATE_FILLED:
                continue
            item = (ss.item or "").replace("_", " ").strip()
            color = (ss.color or "").strip()
            if not item:
                continue
            # Don't re-inject if the body already mentions this item.
            if item.lower() in body_lc:
                continue
            fill = (color, item, slot_name)
            break
        if fill is None:
            continue
        color, item, slot_name = fill
        clothing = f"{color} {item}".strip() if color else item
        # Inject "wearing <clothing>" right after the body-part keyword.
        # End of keyword span = m.end() in `out`; insertion preserves
        # surrounding punctuation/whitespace.
        injection = f" wearing {clothing}"
        out = out[:m.end()] + injection + out[m.end():]
        body_lc = out.lower()
        used_slots.add(slot_name)
    return out


def _inject_missing_pose_facts(body: str, pose: PoseState) -> str:
    """Ensure each pose.descriptive_facts entry survives in the body —
    either as a direct substring or appended as a brief sentence."""
    if not pose.descriptive_facts:
        return body
    body_lc = body.lower()
    missing: list[str] = []
    for fact in pose.descriptive_facts:
        if not isinstance(fact, str):
            continue
        f = fact.strip()
        if not f:
            continue
        if f.lower() in body_lc:
            continue
        missing.append(f)
    if not missing:
        return body
    suffix = " ".join(
        m if m.endswith(".") else f"{m.capitalize()}." for m in missing
    )
    sep = "" if body.rstrip().endswith(".") else "."
    return f"{body.rstrip()}{sep} {suffix}"


def _pose_header(char: CharacterState) -> str:
    """`// Pose: <Name> (signature) from Character: <SourceCharacter>`
    when a bio pose is loaded; `// Pose: <Chip Name>` (no attribution)
    for generic chip applies; `// Pose:` for freeform.

    Bio-pose attribution: when the pose was borrowed from another
    character (e.g. `tifa lockhart in cammy's victory pose`),
    `pose.source_display` is set and the header attributes the pose to
    the source bio (Cammy), not the subject (Tifa). Bio pose without a
    cross-borrow still attributes to the character owner.

    Chip-pose attribution: generic chips (`presenting_feet`) come from
    the curated pool, not from any character's bio — `bio_pose_id` stays
    None to signal this. Header skips the `from Character` suffix so the
    output reads `// Pose: Presenting Feet`."""
    parts = ["// Pose:"]
    pose_name = (char.pose.name or "").strip()
    source = (char.pose.source_display or "").strip()
    char_name = source or (char.display or char.tag or "").strip()
    if pose_name and char.pose.natlang_anchor.strip():
        suffix = pose_name
        if char.pose.is_signature:
            suffix += " (signature)"
        # Attribute "from Character" only for bio-derived poses. Generic
        # pose chips set bio_pose_id=None to opt out of attribution.
        if char.pose.bio_pose_id is not None and char_name:
            suffix += f" from Character: {char_name}"
        parts.append(suffix)
    return " ".join(parts)


def _compose_pose_facts_only(pose: PoseState) -> str:
    """Deterministic fallback when model isn't available. Joins facts
    into a brief sentence."""
    parts = []
    for f in pose.descriptive_facts:
        f = (f or "").strip()
        if f:
            parts.append(f)
    for m in pose.pose_modifiers:
        m = m.replace("_", " ").strip()
        if m:
            parts.append(m)
    if not parts:
        return ""
    body = ", ".join(parts)
    if not body.endswith("."):
        body += "."
    return body[0].upper() + body[1:]


def _build_pose_system_prompt() -> str:
    return (
        "You write a single concise paragraph describing a character's pose "
        "for an image prompt. Use the structured facts as the source of truth. "
        "Do not invent details not in the facts. Output only the paragraph — "
        "no preface, no headers, no quotes."
    )


def _build_pose_user_prompt(pose: PoseState) -> str:
    lines = ["Compose a pose paragraph from these facts:"]
    if pose.natlang_anchor.strip():
        lines.append(f"Bio anchor: {_strip_leading_section_header(pose.natlang_anchor)}")
    if pose.descriptive_facts:
        lines.append("User facts: " + "; ".join(pose.descriptive_facts))
    if pose.pose_modifiers:
        lines.append("Pose modifiers: " + ", ".join(
            m.replace("_", " ") for m in pose.pose_modifiers
        ))
    return "\n".join(lines)


# ── expression / setting (server, plain text) ────────────────────

def render_expression_section(state: PromptState) -> str:
    text = (state.expression or "").strip()
    if not text:
        return ""
    text = _strip_leading_section_header(text)
    if not text.endswith("."):
        text += "."
    text = text[0].upper() + text[1:]
    return f"// Expression:\n{text}"


def render_setting_section(state: PromptState) -> str:
    text = (state.setting or "").strip()
    if not text:
        return ""
    text = _strip_leading_section_header(text)
    # Setting is often a phrase ("in a dungeon at night"). Make it a sentence.
    if not text.endswith("."):
        text += "."
    text = text[0].upper() + text[1:]
    return f"// Scene:\n{text}"


# ── no-bio scene edit (FRUIT for non-character prompts) ──────────

_FRUIT_SCENE_SYSTEM_PROMPT = (
    "You edit a Stable Diffusion prompt's // Scene body. The user has "
    "no curated character bio — the entire scene is freeform descriptive "
    "prose.\n"
    "\n"
    "You will receive:\n"
    "  EXISTING_SCENE: the current scene prose (may be empty)\n"
    "  USER_INSTRUCTION: the user's edit request OR a fresh description\n"
    "\n"
    "Rules:\n"
    "- If EXISTING_SCENE is empty: USER_INSTRUCTION IS the new scene. "
    "Output it verbatim (or trivially cleaned — first letter capitalized, "
    "trailing period). Do NOT elaborate or invent details.\n"
    "- If EXISTING_SCENE is non-empty AND USER_INSTRUCTION is an edit "
    "(`change X to Y`, `remove X`, `add X`, `replace X with Y`): apply "
    "the edit minimally. Preserve every clause USER_INSTRUCTION didn't "
    "touch — typos, capitalization, sentence rhythm. Do NOT rewrite or "
    "polish unaffected parts.\n"
    "- If EXISTING_SCENE is non-empty AND USER_INSTRUCTION is a fresh "
    "description (no edit verb): replace EXISTING_SCENE entirely with "
    "USER_INSTRUCTION.\n"
    "\n"
    "Output ONLY the rewritten scene prose body — no header, no "
    "preamble, no quotes.\n"
    "/no_think"
)


async def edit_scene_no_bio(existing_scene: str,
                             user_instruction: str,
                             *,
                             model_compose: Optional[Callable[[str, str], Awaitable[str]]] = None,
                             ) -> str:
    """FRUIT-style scene edit for non-character prompts. Applies
    user_instruction to existing_scene; on build-mode (no existing) the
    user_instruction becomes the new scene. Falls back to verbatim
    user_instruction when model_compose is unavailable."""
    instr = (user_instruction or "").strip()
    existing = (existing_scene or "").strip()
    if not instr and not existing:
        return ""
    if model_compose is None or not instr:
        return existing or instr
    user_prompt = (
        f"EXISTING_SCENE:\n{existing}\n\n"
        f"USER_INSTRUCTION:\n{instr}\n\n"
        f"Output the rewritten scene prose body."
    )
    try:
        raw = await model_compose(_FRUIT_SCENE_SYSTEM_PROMPT, user_prompt)
        if isinstance(raw, str) and raw.strip():
            return _strip_leading_section_header(raw.strip())
    except Exception:
        pass
    # Fallback: existing prose untouched, or instruction-as-scene if no prior.
    return existing or instr


# ── style / negative passthroughs ────────────────────────────────

def render_style_section(state: PromptState) -> str:
    if state.style is None:
        return ""
    name = (state.style.name or "").strip()
    if not name:
        return ""
    return f"// Style:\n{name}"


# ── orchestrator ─────────────────────────────────────────────────

async def render_all_sections(state: PromptState,
                              *,
                              node_prompt: str = "",
                              changed_sections=None,
                              model_compose=None,
                              ) -> list[dict]:
    """Run every renderer. Sections in `changed_sections` go through
    FRUIT-style regen (existing prose + structured facts → LLM rewrite
    preserving user phrasing). Sections NOT in `changed_sections` are
    preserved verbatim from node_prompt.

    Pass changed_sections=None to render everything (used during
    ingestion / first build when there's no prior prose).

    Style section is rendered by ai_api's post-pass."""
    prior_sections = _parse_prior_sections(node_prompt)

    def should_regen(kind: str) -> bool:
        return changed_sections is None or kind in changed_sections

    def _from_prior_or(kind: str, default: str) -> str:
        if should_regen(kind):
            return default
        prior = prior_sections.get(kind)
        return _section_from_prior(prior) or default

    sections: list[dict] = []

    # Iterate ALL characters in state — multi-character scenarios
    # (`cammy white and chun-li sparring`) emit Character + Outfit
    # blocks per character. Single-character is just len=1 and behaves
    # as before. Prior-section preservation only honors the first
    # // Character / // Outfit block in node_prompt — multi-char on a
    # follow-up turn always re-renders fresh, which is acceptable
    # because preservation can't disambiguate per character without
    # subject-keyed parsing of the prior prose.
    # Filter out phantom characters: matcher false positives can land
    # bios with empty base_natlang in state.characters (e.g. `powered`
    # n-gram from Iron Man's bio body matching `powered_ciel`).
    # render_character_section returns "" for these, but the outfit
    # renderer still emits "Otherwise nude." per character — duplicate
    # empty // Outfit sections in the output. Skip them entirely.
    real_chars = [c for c in state.characters if (c.base_natlang or "").strip()]
    is_multi_char = len(real_chars) > 1
    for idx, ch in enumerate(real_chars):
        is_first = (idx == 0) and not is_multi_char
        # Character — bio.base_natlang verbatim normally; preserve user
        # prose until SwapCharacter (single-char only).
        if is_first:
            char_text = _from_prior_or("character", render_character_section(state, char=ch))
        else:
            char_text = render_character_section(state, char=ch)
        if char_text:
            sections.append(_section_dict(char_text))

        # Outfit — FRUIT-style edit when changed.
        if is_first and should_regen("outfit"):
            existing_outfit = (prior_sections.get("outfit") or {}).get("body_text", "")
            outfit_text = await render_outfit_section(
                state, existing_outfit, model_compose=model_compose, char=ch,
            )
        elif is_first:
            outfit_text = _from_prior_or(
                "outfit",
                await render_outfit_section(state, "", model_compose=model_compose, char=ch),
            )
        else:
            # Secondary character outfits always regen fresh — there's
            # no prior-prose-by-character map to preserve from.
            outfit_text = await render_outfit_section(
                state, "", model_compose=model_compose, char=ch,
            )
        if outfit_text:
            sections.append(_section_dict(outfit_text))

    # Pose — LLM regen when changed.
    if should_regen("pose"):
        pose_text = await render_pose_section(state, model_compose=model_compose)
    else:
        pose_text = _from_prior_or(
            "pose",
            await render_pose_section(state, model_compose=model_compose),
        )
    if pose_text:
        sections.append(_section_dict(pose_text))

    # Expression — preserve verbatim when not changed.
    expr_text = _from_prior_or("expression", render_expression_section(state))
    if expr_text:
        sections.append(_section_dict(expr_text))

    # Setting — same. Header may be either `// Scene:` or `// Setting:`.
    if should_regen("setting"):
        set_text = render_setting_section(state)
    else:
        prior = prior_sections.get("scene") or prior_sections.get("setting")
        set_text = _section_from_prior(prior) or render_setting_section(state)
    if set_text:
        sections.append(_section_dict(set_text))

    return sections


def _parse_prior_sections(node_prompt: str) -> dict:
    """Extract section bodies from a // Section-formatted node_prompt.
    Returns {kind: {header, body_text}} where kind is the lowercased
    first word of the section header (character/outfit/pose/expression/
    scene/style)."""
    if not node_prompt:
        return {}
    out: dict = {}
    current_kind = None
    current_header = ""
    current_body: list[str] = []
    for line in node_prompt.splitlines():
        if line.strip().startswith("//"):
            if current_kind:
                out[current_kind] = {
                    "header": current_header,
                    "body_text": _NL.join(current_body).strip(),
                }
            current_header = line.rstrip()
            inner = line.strip().lstrip("/").strip()
            kind_word = inner.split(":", 1)[0].strip().lower().split()[0] if inner else ""
            if kind_word in ("character", "outfit", "pose", "expression", "scene", "setting", "style"):
                current_kind = kind_word
            else:
                current_kind = None
            current_body = []
        elif current_kind:
            current_body.append(line)
    if current_kind:
        out[current_kind] = {
            "header": current_header,
            "body_text": _NL.join(current_body).strip(),
        }
    return out


def _section_from_prior(prior) -> str:
    """Return header+newline+body from a prior section dict, or empty."""
    if not prior:
        return ""
    header = (prior.get("header") or "").strip()
    body = (prior.get("body_text") or "").strip()
    if not header or not body:
        return ""
    return header + _NL + body


def _section_dict(rendered: str) -> dict:
    """Split a rendered "// Header:\nbody" string into the section dict
    shape used by the rest of ai_api."""
    lines = rendered.split("\n", 1)
    header = lines[0].rstrip()
    body = lines[1].strip() if len(lines) > 1 else ""
    return {
        "header": header,
        "body_text": body,
        "tokens": [body] if body else [],
    }


# ── helpers ──────────────────────────────────────────────────────

_LEADING_SECTION_HEADER_RE = re.compile(r"^\s*//\s*[A-Za-z][^\n]*\n+")


def _strip_leading_section_header(text: str) -> str:
    if not text:
        return text
    return _LEADING_SECTION_HEADER_RE.sub("", text).strip()
