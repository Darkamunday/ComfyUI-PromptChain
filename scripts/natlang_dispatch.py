"""Dispatcher — pure Python (no LLM calls).

Reads scan output + decompose intent and produces the SEARCH /
REPLACE / INSERT_AFTER triple for apply. Two classes of concept get
different routing:

  Top-level sentence concepts (one per prompt):
    character, pose, expression, scene, style, quality
  - Present in scan → SEARCH=scan[concept], REPLACE=formatted text
  - Absent → SEARCH=(not present), INSERT_AFTER=canonical-before lookup

  Sub-concepts (live inside the outfit clause):
    tops, bottoms, footwear, legwear, handwear, armwear, headwear,
    neckwear, accessories
  - Route through locate-infill but ONLY on the outfit sentence, not
    the full prompt. Tight scope, model can't grab whole prose.

  Sub-concepts of character (hair, eyes, body, face):
  - Same idea on the character sentence.

  Outfit (meta — whole-outfit swap):
  - Present → SEARCH=scan['outfit'], REPLACE=resolved outfit text
  - Absent → INSERT_AFTER canonical-before
"""
from __future__ import annotations

from typing import Optional


# Canonical order. Index = position in the prompt.
CANONICAL_ORDER = (
    "character",
    "outfit",
    "pose",
    "expression",
    "scene",
    "style",
    "quality",
)

# Decompose vocabulary differs from dispatch vocabulary — `subject`
# from decompose maps to `character` here. Other concept names already
# align.
_CONCEPT_ALIASES = {
    "subject": "character",
}


def normalize_concept(concept: str) -> str:
    """Map decompose concept names onto dispatch's canonical vocabulary."""
    return _CONCEPT_ALIASES.get(concept, concept)


# Sentence-shaped concepts — they get their own sentence in the prompt.
SENTENCE_CONCEPTS = {
    "character", "pose", "expression", "scene", "style", "quality",
    "outfit",  # outfit is sentence-shaped at the whole-swap level
}

# Sub-concepts that live inside the outfit sentence.
OUTFIT_SUBSLOTS = {
    "tops", "bottoms", "footwear", "legwear", "handwear",
    "armwear", "headwear", "neckwear", "accessories",
}

# Sub-concepts that live inside the character sentence.
CHARACTER_SUBSLOTS = {
    "hair", "eyes", "body", "face",
}


def parent_concept(concept: str) -> Optional[str]:
    """Map a sub-concept to its parent sentence concept."""
    if concept in OUTFIT_SUBSLOTS:
        return "outfit"
    if concept in CHARACTER_SUBSLOTS:
        return "character"
    if concept in SENTENCE_CONCEPTS:
        return concept
    return None


def canonical_anchor(concept: str, scan: dict) -> Optional[str]:
    """Walk BACK from `concept` through CANONICAL_ORDER and return the
    sentence of the most recent present concept. None if nothing is
    before this concept.
    """
    try:
        idx = CANONICAL_ORDER.index(concept)
    except ValueError:
        return None
    for prev in CANONICAL_ORDER[:idx][::-1]:
        v = (scan.get(prev) or "").strip()
        if v:
            return v
    return None


def format_replace(concept: str, text: str,
                   verbatim: bool = False) -> str:
    """Mechanical formatting for inserted prose. Sentence-shaped
    concepts get capitalized + period-terminated; sub-concepts (items)
    stay verbatim.

    Set `verbatim=True` for KB-resolved bodies (compose_*_natlang_v2,
    style template body, scene base_natlang) — those come from the
    DB already formatted exactly the way legacy emits them, so the
    cap+period rule would just mangle them. The cap+period rule
    exists for raw user text and vibe output, not for KB bodies.
    """
    text = (text or "").strip()
    if not text:
        return text
    if verbatim:
        return text
    if concept in SENTENCE_CONCEPTS:
        if not text[0].isupper():
            text = text[0].upper() + text[1:]
        if not text.endswith((".", "!", "?")):
            text = text + "."
    return text


def format_section_header(concept: str,
                          resolved_match: Optional[dict] = None,
                          resolved_source: Optional[str] = None) -> str:
    """Build a `// Section: <metadata>` header for a sentence-shaped
    concept. Mirrors legacy `core/natlang_render_v2.py` formats. When
    resolved_match is absent (vibe path or raw text), emits the bare
    `// Section:` form.

    Returns empty string for non-sentence concepts.
    """
    if concept not in SENTENCE_CONCEPTS:
        return ""

    m = resolved_match or {}

    if concept == "character":
        display = (m.get("display") or m.get("tag") or "").strip()
        series = (m.get("series") or "").strip()
        if display:
            return f"// Character: {display} ({series})" if series \
                else f"// Character: {display}"
        return "// Character:"

    if concept == "outfit":
        outfit_name = (m.get("outfit_name") or m.get("name") or "").strip()
        char_display = (m.get("character_display") or "").strip()
        if outfit_name and char_display:
            return f"// Outfit: {outfit_name} from Character: {char_display}"
        if outfit_name:
            return f"// Outfit: {outfit_name}"
        return "// Outfit:"

    if concept == "pose":
        pose_name = (m.get("pose_name") or m.get("name") or "").strip()
        is_signature = bool(m.get("is_signature"))
        char_display = (m.get("character_display") or "").strip()
        if pose_name:
            suffix = pose_name
            if is_signature:
                suffix += " (signature)"
            if char_display:
                suffix += f" from Character: {char_display}"
            return f"// Pose: {suffix}"
        return "// Pose:"

    if concept == "scene":
        scene_name = (m.get("display_name") or m.get("name") or "").strip()
        # Legacy renders this header as `// Setting:` in some paths and
        # `// Scene:` in others — stick with `// Scene:` for parity with
        # scan_prompt's concept name.
        if scene_name:
            return f"// Scene: {scene_name}"
        return "// Scene:"

    if concept == "style":
        # `template_name` is the new field; `name` stays for older
        # callers that pass an outfit/scene-shaped dict.
        style_name = (m.get("template_name") or m.get("name") or "").strip()
        if style_name:
            return f"// Style: {style_name}"
        return "// Style:"

    if concept == "expression":
        return "// Expression:"

    if concept == "quality":
        return "// Quality:"

    return ""


def dispatch_sentence_concept(intent: dict, scan: dict,
                              resolved_match: Optional[dict] = None,
                              resolved_source: Optional[str] = None,
                              emit_headers: bool = True) -> dict:
    """For pose / expression / scene / style / quality / character /
    outfit: produce SEARCH/REPLACE/INSERT_AFTER deterministically from
    scan output and canonical order.

    When the path is INSERT (concept absent from prompt) and the
    surrounding prompt is sectioned (`emit_headers=True`), prepend a
    `// Section:` header so the brand-new section arrives with its
    metadata. For flat-prose prompts (`emit_headers=False`), emit a
    bare body sentence that blends into the existing prose stream.

    REPLACE (concept's sentence is already in the prompt) always emits
    bare body — any existing header above the matched sentence stays
    in place regardless of mode.
    """
    concept = normalize_concept(intent["concept"])
    op = (intent.get("op") or "replace").lower()
    text = intent.get("resolved_text") or intent.get("text") or ""
    # KB-resolved bodies (`resolved_source` set) pass through
    # verbatim — they're already the exact prose legacy emits.
    body = format_replace(concept, text, verbatim=bool(resolved_source))

    present = (scan.get(concept) or "").strip()
    # op=add explicitly means "insert another section of this concept
    # next to the existing one" (e.g. multi-character build:
    # subject=cammy + subject=tifa). Force INSERT even when scan
    # already has the concept — REPLACE would clobber the prior char's
    # section.
    if present and op != "add":
        return {
            "search": present,
            "replace": body,
            "insert_after": None,
            "method_hint": "sentence_replace",
        }
    if emit_headers:
        header = format_section_header(concept, resolved_match, resolved_source)
        replace = f"{header}\n{body}" if header and body else body
    else:
        replace = body
    # When op=add and the concept is already present, anchor AFTER the
    # existing body of this same concept so the new section lands
    # immediately after the prior one (e.g. // Character: Tifa lands
    # after // Character: Cammy in multi-char build). Falls through
    # to canonical_anchor when the concept isn't present yet.
    if present and op == "add":
        anchor = present
    else:
        anchor = canonical_anchor(concept, scan)
    return {
        "search": "(not present)",
        "replace": replace,
        "insert_after": anchor or "",
        "method_hint": "sentence_insert",
    }


def dispatch(intent: dict, scan: dict,
             resolved_match: Optional[dict] = None,
             resolved_source: Optional[str] = None,
             emit_headers: bool = True) -> dict:
    """Top-level dispatch given a decompose intent + scan result.

    Returns dict with:
      - kind: 'sentence' (use Python) or 'subslot' (use locate-infill)
      - parent_sentence: for subslot kind, the sentence to scope locate-infill to
      - parsed: for sentence kind, the {search, replace, insert_after} triple
    """
    concept = normalize_concept(intent["concept"])
    if concept in SENTENCE_CONCEPTS:
        return {
            "kind": "sentence",
            "concept": concept,
            "parsed": dispatch_sentence_concept(
                intent, scan,
                resolved_match=resolved_match,
                resolved_source=resolved_source,
                emit_headers=emit_headers,
            ),
        }
    parent = parent_concept(concept)
    if parent is None:
        # Unknown concept — fall back to locate-infill on full prompt
        return {
            "kind": "subslot",
            "concept": concept,
            "parent": None,
            "parent_sentence": None,
        }
    parent_sentence = (scan.get(parent) or "").strip()
    return {
        "kind": "subslot",
        "concept": concept,
        "parent": parent,
        "parent_sentence": parent_sentence or None,
    }
