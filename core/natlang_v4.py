"""Natlang v4 -- identify-then-rewrite render path for natlang patches.

Replaces `render_all_sections` for the "non-swap" case: when v2's
decompose produced no swap-shape deltas (SwapOutfit, SwapCharacter,
SwapPose, ApplyPoseChip, SwapStyle, ClearX), v2's render rebuilds
section bodies from state -- which is destructive when the prompt has
rich modifier prose that state doesn't track (insignia descriptions,
leg-opening modifiers, chest harness details, etc.).

v4 sidesteps state-driven render entirely for that case. Three stages:

  Stage 0 -- preprocess(node_prompt) -> tagged_lines, section_map:
    Parse `// Section:` headers, split each section body on `\\n`, `.`,
    `,` into atomic clauses. Tag each clause with `[CHAR]` / `[OUTFIT]`
    / `[POSE]` / `[EXPR]` / `[SETTING]` / `[STYLE]`. Preserve per-clause
    trailing separator so reassemble can restore the original prose
    shape exactly (period/comma/newline boundaries survive).

  Stage 1 -- split_intents(user_request) -> list[str]:
    LLM call splits compound requests into atomic intents.

  Stage 2 (per intent) -- identify_clauses + rewrite_clauses:
    LLM call 1: identify which tagged clauses are relevant. Verbatim
    substring constraint -- model returns clauses copied exactly from
    the input list with their [TAG] prefix. Out-of-band sentinels:
    NEW_CONTENT (request adds content with no place to attach it) /
    NONE (no change needed).
    LLM call 2: rewrite each identified clause. Tag prefix preserved
    in output. Empty body after the tag = delete that clause.

  Stage 3 -- reassemble(edited_tagged_lines, section_map) -> str:
    Strip tags, restore per-clause separators, re-emit `// Section:`
    headers. Sections the model didn't touch are byte-identical to
    input. Non-destructive by algorithm, not by trusting the LLM to
    preserve.

This module is invoked from `_run_natlang_v2` after deltas are computed.
v2's state mutation still runs (FillSlotDelta etc. update PromptState
for TagBuilder UI). v4 just replaces the body-rendering step.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional


_HEADER_KIND_TO_TAG = {
    "character": "[CHAR]",
    "outfit": "[OUTFIT]",
    "pose": "[POSE]",
    "expression": "[EXPR]",
    "setting": "[SETTING]",
    "scene": "[SETTING]",
    "style": "[STYLE]",
}

_TAG_PREFIX_RE = re.compile(r"^\s*(\[[A-Z]+\])\s+(.*)$")
# Bare-tag form: just `[OUTFIT]` or `[OUTFIT]\s*` with nothing after.
# Used by the rewrite step to signal "delete this clause" -- we map it
# to empty text so the survivors filter in reassemble drops it.
_BARE_TAG_RE = re.compile(r"^\s*\[[A-Z]+\]\s*$")


# ── Stage 0: preprocess + reassemble ───────────────────────────────


def _section_tag(header: str) -> str:
    m = re.match(r"^\s*//\s*(\w+)", header)
    if not m:
        return "[UNKNOWN]"
    return _HEADER_KIND_TO_TAG.get(m.group(1).lower(), "[UNKNOWN]")


def _split_body_preserve_separators(body: str) -> list[dict]:
    """Split a section body into clauses while remembering each clause's
    trailing separator (`,`, `.`, `\\n`, or `""` for the last clause).
    Returns list of {text, sep} dicts."""
    if not body or not body.strip():
        return []
    parts = re.split(r"([,.\n])", body)
    out: list[dict] = []
    i = 0
    while i < len(parts):
        text = parts[i].strip() if i < len(parts) else ""
        sep = parts[i + 1] if (i + 1) < len(parts) else ""
        if text:
            out.append({"text": text, "sep": sep})
        i += 2
    return out


def preprocess(prompt: str) -> tuple[list[str], dict]:
    """Returns (tagged_lines, section_map).

    Each line in tagged_lines is `[TAG] clause`. section_map carries
    the per-section + per-clause data reassemble() needs."""
    parts = re.split(r"(?m)^(\s*//\s*[^\n]+)$", prompt)
    sections: list[dict] = []
    if parts and parts[0].strip():
        sections.append({"header": "", "body_raw": parts[0]})
    i = 1
    while i < len(parts):
        header = parts[i].strip()
        body = parts[i + 1] if (i + 1) < len(parts) else ""
        sections.append({"header": header, "body_raw": body})
        i += 2

    tagged_lines: list[str] = []
    for sec_idx, sec in enumerate(sections):
        header = sec["header"]
        tag = _section_tag(header) if header else "[UNKNOWN]"
        sec["tag"] = tag
        body = sec["body_raw"]
        neg_match = re.search(r"^\s*Negative Prompt:\s*$", body, re.MULTILINE)
        body_before_neg = body[:neg_match.start()] if neg_match else body
        neg_block = body[neg_match.start():] if neg_match else ""
        clauses_with_seps = _split_body_preserve_separators(body_before_neg)
        sec["neg_block"] = neg_block.strip()
        sec["clauses"] = clauses_with_seps
        sec["clause_indices"] = []
        for c in clauses_with_seps:
            tagged_lines.append(f"{tag} {c['text']}")
            sec["clause_indices"].append(len(tagged_lines) - 1)
    return tagged_lines, {"sections": sections, "tagged_lines": tagged_lines}


def _strip_tag(line: str) -> str:
    # Bare `[TAG]` (delete signal from rewrite) -> empty string so the
    # reassemble survivors filter drops the clause entirely.
    if _BARE_TAG_RE.match(line):
        return ""
    m = _TAG_PREFIX_RE.match(line)
    return m.group(2).strip() if m else line.strip()


def _join_clauses_with_separators(items: list[dict]) -> str:
    survivors = [it for it in items if it.get("text", "").strip()]
    if not survivors:
        return ""
    pieces: list[str] = []
    for i, it in enumerate(survivors):
        text = it["text"].strip()
        is_last = (i == len(survivors) - 1)
        sep = it.get("sep", "")
        if is_last:
            if sep == ".":
                pieces.append(text + ".")
            else:
                pieces.append(text)
            continue
        if sep == "\n":
            pieces.append(text + "\n")
        elif sep == ".":
            pieces.append(text + ". ")
        elif sep == ",":
            pieces.append(text + ", ")
        else:
            pieces.append(text + " ")
    return "".join(pieces).rstrip()


def reassemble(edited_tagged_lines: list[str], smap: dict) -> str:
    sections = smap["sections"]
    out: list[str] = []
    for sec in sections:
        header = sec["header"]
        clause_indices = sec["clause_indices"]
        sec_clauses = sec.get("clauses") or []
        edited_items: list[dict] = []
        for idx, original in zip(clause_indices, sec_clauses):
            line = edited_tagged_lines[idx]
            new_text = _strip_tag(line)
            edited_items.append({"text": new_text, "sep": original.get("sep", "")})
        body = _join_clauses_with_separators(edited_items)
        neg = sec.get("neg_block") or ""
        if header:
            out.append(header)
        if body.strip():
            out.append(body)
        if neg:
            out.append("")
            out.append(neg)
        out.append("")
    return "\n".join(out).strip()


# ── Stage 1: intent splitter ────────────────────────────────────────

SPLIT_SYSTEM = """You split image-prompt edit requests into atomic single-intent requests.

An atomic intent is ONE change. "bigger feet and blue eyes" is two intents; "longer hair" is one.

Output rules:
  - One atomic intent per line.
  - Drop only conjunctions (and / with / , / +); keep the rest verbatim.
  - No preamble, no markdown.
  - If the request is already atomic, output one line.
  - If empty or no actionable change, output nothing."""


# ── Stage 2: identify + rewrite ────────────────────────────────────

IDENTIFY_SYSTEM = """You inspect a list of image-prompt clauses, each prefixed with a section tag, and identify which clauses need to change to satisfy a user's atomic edit request. You do NOT rewrite anything.

Section tag meanings:
  [CHAR]    -- physical features of the character (body parts, build, hair, eyes, skin)
  [OUTFIT]  -- clothing, accessories, footwear
  [POSE]    -- body position, gestures, gaze, what body parts are presented
  [EXPR]    -- facial affect
  [SETTING] -- environment, location, background, mood
  [STYLE]   -- rendering style or aesthetic

Routing by request type (use the tags to pick the right clauses):
  - Anatomy modifications (bigger feet, longer hair, broader shoulders) -> [CHAR] clauses ONLY. Do not touch [OUTFIT] clauses just because they mention foot-adjacent items.
  - Clothing add/remove/color -> [OUTFIT] clauses.
  - Body-state modifiers (barefoot, topless, nude) -> [OUTFIT] clauses.
  - Pose / posture changes -> [POSE] clauses.
  - Expression / facial -> [EXPR] clauses.
  - Setting / scene -> [SETTING] clauses.
  - Style -> [STYLE] clauses.

Strict output rules:
  - Output the relevant clauses, one per line, each copied VERBATIM from the input INCLUDING the [TAG] prefix.
  - Pick the FEWEST clauses that satisfy the request. Usually ONE.
  - If anatomy mod targets a body part not yet mentioned in any [CHAR] clause, pick the [CHAR] clause describing general physique/build as the extension target.
  - If no clause is relevant and no clause is a reasonable extension target (e.g., request needs a [SETTING] clause but none exist), output exactly: NEW_CONTENT
  - If the request requires no change, output exactly: NONE
  - No preamble, no commentary, no markdown."""


REWRITE_SYSTEM = """You rewrite selected clauses of an image prompt to satisfy a user's atomic edit request.

Each input clause begins with a section tag like [CHAR] or [OUTFIT]. You MUST keep the SAME tag in your replacement (the tag identifies which section the replacement belongs to).

Strict output rules:
  - Output the replacement clauses under `--- CLAUSE N ---` headers in the same order as inputs.
  - Preserve the original [TAG] prefix at the start of each replacement.
  - Preserve clause-style prose (a short comma-list clause stays comma-list-style; a sentence stays a sentence).
  - Apply only the minimum change the user asked for.

Operation cases (in priority order):

  COLOR / QUALIFIER CHANGE -- when the user's intent is `<color> <item>` or `<adjective> <item>` and the input clause already contains that item with a different color/qualifier:
    REPLACE the color/qualifier IN PLACE. Keep everything else in the clause.
    Examples:
      intent "blue gloves" + clause "[OUTFIT] red gloves" -> "[OUTFIT] blue gloves"
      intent "pink leotard" + clause "[OUTFIT] Wearing green sleeveless thong leotard with insignia"
        -> "[OUTFIT] Wearing pink sleeveless thong leotard with insignia"
      intent "longer hair" + clause "[CHAR] long hair" -> "[CHAR] longer hair"
    Do NOT delete the clause. Do NOT add a duplicate clause.

  ADDITIVE -- when the user asks to add a feature/item that isn't present in any input clause:
    Extend the clause inline. Examples:
      intent "bigger feet" + clause "[CHAR] toned athletic female body" -> "[CHAR] toned athletic female body with bigger feet"
      intent "red socks" + clause "[OUTFIT] Barefoot" -> "[OUTFIT] Barefoot, red socks"

  REMOVAL -- when the user asks to REMOVE / DROP / GET RID OF / TAKE OFF an item:
    Output the [TAG] prefix on its own line with NOTHING after it -- e.g., just `[OUTFIT]`. The reassembler drops empty clauses.
    The item to remove may appear inside a compound noun (target=boots strips "combat boots", "calf-high boots"; target=gloves strips "fingerless gloves"). Strip the entire noun phrase including color/size adjectives.
    Only output an empty `[TAG]` when the user EXPLICITLY asked to remove. If the user asked to change a color or replace an item, use COLOR / QUALIFIER CHANGE or ADDITIVE instead.

No commentary, no markdown fences."""


def _parse_identify(raw: str, tagged_lines: list[str]) -> tuple[list[str], str]:
    text = (raw or "").strip()
    if not text:
        return [], "parse_fail"
    if text.startswith("```"):
        ls = text.splitlines()
        if ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        text = "\n".join(ls).strip()
    if text == "NEW_CONTENT":
        return [], "NEW_CONTENT"
    if text == "NONE":
        return [], "NONE"
    spans = [s.strip() for s in text.splitlines() if s.strip()]
    if not spans:
        return [], "parse_fail"
    line_set = set(tagged_lines)
    if any(s not in line_set for s in spans):
        return spans, "parse_fail"
    return spans, "ok"


def _parse_rewrite(raw: str, n: int) -> tuple[list[str], str]:
    text = (raw or "").strip()
    if not text:
        return [], "parse_fail"
    parts = re.split(r"(?m)^---\s*CLAUSE\s*\d+\s*---\s*$", text)
    parts = [p.strip() for p in parts]
    if parts and not parts[0]:
        parts = parts[1:]
    if len(parts) != n:
        return parts, "count_mismatch"
    return parts, "ok"


async def split_intents(user_request: str, model_compose) -> list[str]:
    """model_compose: async (system_prompt, user_msg) -> str."""
    raw = await model_compose(SPLIT_SYSTEM, f"User edit request: {user_request}")
    return [s.strip() for s in (raw or "").splitlines() if s.strip()]


# Anatomy classification: size/shape adjective + body part. Used to
# programmatically constrain identify_clauses to [CHAR]-only when the
# intent is unambiguously anatomy -- 8B otherwise picks foot-adjacent
# [OUTFIT] clauses (like "Barefoot") for "bigger feet". This is
# generic linguistic shape (adjective + noun), not a fixture rule.
_ANATOMY_ADJECTIVES = (
    "bigger", "larger", "smaller", "tinier", "huge", "huger", "tiny",
    "longer", "shorter",
    "wider", "narrower", "broader",
    "thicker", "thinner", "skinnier",
    "oversized", "enlarged", "massive", "petite",
)
_ANATOMY_BODY_PARTS = (
    "feet", "foot", "soles", "toe", "toes",
    "hair", "hairs",
    "hand", "hands", "fingers",
    "shoulder", "shoulders",
    "waist", "hip", "hips",
    "leg", "legs", "thigh", "thighs", "calves",
    "arm", "arms",
    "eye", "eyes", "lip", "lips",
    "breast", "breasts", "chest", "bust",
    "ear", "ears", "neck", "nose", "mouth",
    "butt", "ass", "rear",
)


def classify_intent_target_tag(intent: str) -> Optional[str]:
    """If the intent unambiguously targets one section (e.g. anatomy
    modification -> [CHAR]), return that tag. Else return None and let
    the LLM identify freely."""
    if not intent:
        return None
    intent_lc = intent.lower()
    # Word-boundary match so "ear" doesn't match "wear" / "earrings".
    words = set(re.findall(r"\b\w+\b", intent_lc))
    has_adj = bool(words & set(_ANATOMY_ADJECTIVES))
    has_part = bool(words & set(_ANATOMY_BODY_PARTS))
    if has_adj and has_part:
        return "[CHAR]"
    return None


async def identify_clauses(tagged_lines: list[str],
                           atomic_request: str,
                           model_compose) -> tuple[list[str], str]:
    # Programmatic section constraint: when the intent unambiguously
    # targets one section (e.g. anatomy mod -> [CHAR]), filter the
    # tagged_lines to that section before showing them to the LLM.
    # Prevents 8B from picking foot-adjacent [OUTFIT] clauses like
    # "Barefoot" as the target for "bigger feet" -- the LLM literally
    # can't see [OUTFIT] clauses in that case, only [CHAR] ones.
    target_tag = classify_intent_target_tag(atomic_request)
    scoped_lines = tagged_lines
    if target_tag:
        filtered = [l for l in tagged_lines if l.lstrip().startswith(target_tag)]
        # Fall back to all lines if filter empties the input (no
        # [CHAR] clauses present -- shouldn't happen in practice but
        # don't trap identify in an impossible state).
        if filtered:
            scoped_lines = filtered
    user_msg = (
        f"User atomic request: {atomic_request}\n\n"
        f"Clauses (one per line, prefixed with section tag):\n"
        + "\n".join(scoped_lines)
    )
    raw = await model_compose(IDENTIFY_SYSTEM, user_msg)
    # Validate against the ORIGINAL tagged_lines (so substitution
    # downstream still works -- _parse_identify checks substring
    # presence in the full list).
    return _parse_identify(raw, tagged_lines)


async def rewrite_clauses(spans: list[str],
                          atomic_request: str,
                          model_compose) -> tuple[list[str], str]:
    parts = []
    for i, s in enumerate(spans, 1):
        parts.append(f"--- CLAUSE {i} ---\n{s}")
    user_msg = (
        f"User atomic request: {atomic_request}\n\n"
        f"Clauses to rewrite (output replacement under matching header, "
        f"preserving the [TAG] prefix):\n\n"
        + "\n\n".join(parts)
    )
    raw = await model_compose(REWRITE_SYSTEM, user_msg)
    return _parse_rewrite(raw, len(spans))


async def edit_prompt(prompt: str, user_request: str, model_compose) -> dict:
    """Full pipeline. Returns {output, intents, edits, statuses}."""
    tagged, smap = preprocess(prompt)
    intents = await split_intents(user_request, model_compose)
    if not intents:
        return {"output": prompt, "intents": [], "edits": [], "statuses": []}
    edited = list(tagged)
    edits: list[dict] = []
    statuses: list[str] = []
    for atomic in intents:
        spans, ident_status = await identify_clauses(edited, atomic, model_compose)
        if ident_status in ("NEW_CONTENT", "NONE"):
            edits.append({"intent": atomic, "spans": [], "replacements": [],
                          "status": ident_status})
            statuses.append(ident_status)
            continue
        if ident_status == "parse_fail":
            edits.append({"intent": atomic, "spans": spans, "replacements": [],
                          "status": "identify_parse_fail"})
            statuses.append("identify_parse_fail")
            continue
        reps, rew_status = await rewrite_clauses(spans, atomic, model_compose)
        if rew_status != "ok":
            edits.append({"intent": atomic, "spans": spans,
                          "replacements": reps, "status": f"rewrite_{rew_status}"})
            statuses.append(f"rewrite_{rew_status}")
            continue
        for orig, new in zip(spans, reps):
            try:
                idx = edited.index(orig)
                edited[idx] = new
            except ValueError:
                pass
        edits.append({"intent": atomic, "spans": spans, "replacements": reps,
                      "status": "ok"})
        statuses.append("ok")
    output = reassemble(edited, smap)
    return {"output": output, "intents": intents, "edits": edits,
            "statuses": statuses}


# ── delta classification for the swap-shape gate ───────────────────


def has_swap_shape_delta(deltas: list) -> bool:
    """Decide whether v2's state-driven render should run or whether
    v4 identify-rewrite should take over.

    Swap-shape deltas (SwapOutfit/SwapCharacter/SwapPose/ApplyPoseChip/
    SwapStyle/ClearX) carry a KB-anchored verbatim payload that v2's
    render is best at surfacing. Non-swap deltas (FillSlot/ClearSlot/
    PoseChange/SetExpression/SetSetting) are exactly where v2's render
    becomes destructive -- state doesn't track rich modifier prose,
    so the FRUIT recompose drops it. Those cases route to v4 instead.

    Empty deltas (no delta type produced; common for anatomy mods that
    v2 has no delta type for) also route to v4."""
    if not deltas:
        return False
    from . import natlang_facts as _nlf
    swap_types = (
        _nlf.SwapCharacterDelta,
        _nlf.SwapOutfitDelta,
        _nlf.SwapPoseDelta,
        _nlf.ApplyPoseChipDelta,
        _nlf.SwapStyleDelta,
        _nlf.ApplyModifierDelta,  # modifier brings curated substitute_section + clears_slots
        _nlf.StripDelta,
        _nlf.ClearSettingDelta,
        _nlf.ClearExpressionDelta,
        _nlf.ClearPoseDelta,
        _nlf.ClearStyleDelta,
    )
    return any(isinstance(d, swap_types) for d in deltas)
