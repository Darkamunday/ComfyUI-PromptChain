"""Tag-rails v1 — deterministic compose pipeline for tag mode.

Replaces the monolithic `_api_patch` patch-generation LLM call with:

  decompose -> resolve -> compose (deterministic) -> coherence (deterministic)

The LLM is involved only in decompose (intent extraction) and resolve
(per-intent canonical tag lookup). Section assembly, multi-character
composition, outfit borrow, modifier cascades, and the 8 SDXL anti-
burn-in strategies all run server-side as named functions.

See dev-promptchain/docs/plans/tag-rails-migration-plan.md for the full
architecture rationale.

Public entry point:
  run_tag_rails(node_prompt, user_request, bios, *, model_hash, ...) -> dict
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Optional

from core import ai_api as _api


logger = logging.getLogger("promptchain.ai.tag_rails")


PROVIDER_DEFAULT = "local"
CONFIG_DEFAULT = {"local": {"base_url": "http://localhost:11434/v1",
                              "model": "qwen3-vl:8b-instruct"}}


# Narrow per-section rewrite prompt — natlang's REWRITE_SYSTEM adapted
# for tag mode. The LLM sees ONE section + ONE intent + retrieval
# candidates and emits the updated token list. No knowledge of other
# sections, no PATCH MODE rules, no SECTION STRUCTURE template — just
# "modify this list to apply this change."
TAG_SECTION_REWRITE_SYSTEM = """You are editing one section of a Stable Diffusion tag-mode prompt.

You receive:
  CURRENT TOKENS — the section's current comma-separated tokens.
  INTENT — what the user wants done to this section.
  CANDIDATES — canonical Danbooru tags that may be relevant.

Your job: produce the updated comma-separated token list. NOTHING ELSE.

Rules:
  1. Output canonical Danbooru tags ONLY. Underscored, lowercase, in
     CANONICAL WORD ORDER (Danbooru convention is usually
     <verb>_<body_part> or <attribute>_<noun>: `spread_toes`, not
     `toes_spread`; `looking_at_viewer`, not `viewer_looking`;
     `red_socks`, not `socks_red`; `foot_focus`, not `focus_foot`).
     If a CANDIDATE matches the concept, use it verbatim. Composition
     is OK (red_socks, polka_dot_socks, blue_leotard) — SD tokenizes
     compounds correctly. Never emit phrasal English or section headers.
  2. Keep tokens the user didn't ask to change. Preserve verbatim.
  3. Apply the intent. Common patterns:
       - ADD X: append X if not present. If X conflicts with an
         existing token (e.g. red_socks conflicts with barefoot),
         drop the conflicting token AND add X.
       - REMOVE X: drop X if present.
       - REPLACE X with Y: swap X out for Y.
       - MODIFY existing token (e.g. "make her gloves blue"): replace
         the existing color qualifier on the slot (red_gloves ->
         blue_gloves).
       - EXPAND / ADD MORE: append 4-6 canonical tags from CANDIDATES
         that fit the section's theme. Don't invent — only use
         CANDIDATES or canonical tags you're sure exist.
       - REPLACE POSTURE (e.g. "lying down" when current has sitting):
         drop the conflicting posture tokens, add the new posture.
  4. Slot exclusivity for body-state modifiers:
       barefoot / bareheaded / topless / nude / completely_nude
     If the user adds an item to a covered slot (e.g. socks), drop the
     conflicting body-state modifier (barefoot).
  5. NEVER write `// Section: name` headers as tokens.
  6. NEVER write the literal user intent text as a token.

Output ONLY the updated comma-separated token list. No prefix, no
quotes, no commentary, no markdown.

If you don't see how the intent applies to this section, output the
single literal token: UNCHANGED
"""


TAG_SCAN_SYSTEM = """You are a structural classifier. You receive a flat comma-separated Stable Diffusion tag list (no section headers, no rails markup) and label each tag with one concept so downstream code can route them.

The classification vocabulary — these are the ONLY labels you may use:
  character   — named subject token (e.g. `cammy_white`, `(tifa_lockhart:1.1)`, `1girl`, `2girls`), and physical traits permanently belonging to the body (hair color/length/style, eye color, skin tone, body build, breast/bust size, scars, freckles)
  outfit      — clothing, footwear, gloves, hat, accessories, body-paint, body-state modifiers (`barefoot`, `topless`, `nude`)
  pose        — body position, action, gesture, gaze direction (sitting, standing, looking_at_viewer, presenting, legs_up)
  expression  — facial affect (smiling, frowning, sultry, neutral_face)
  setting     — environment, location, background, weather, lighting tied to the scene (forest, beach, sunset, indoors)
  style       — rendering aesthetic (anime, photorealistic, oil_painting, cinematic, watercolor)
  quality     — meta/quality tokens (masterpiece, best_quality, sharp_focus, depth_of_field, lowres_negative_indicator)

Important distinctions:
  - `(cammy_white:1.1)` is CHARACTER (the parenthesised name+weight is the identity marker).
  - `1girl` / `2girls` are CHARACTER (subject count).
  - `barefoot` / `topless` are OUTFIT (worn-state markers), not pose.
  - `masterpiece` / `best_quality` are QUALITY, not style.
  - `anime` / `photorealistic` are STYLE.
  - Lighting: `cinematic_lighting` is STYLE, `sunset` is SETTING.

Output format — ONE LINE per concept that has at least one tag, with the verbatim tokens comma-separated, preserving order:

  character: <tag>, <tag>, ...
  outfit: <tag>, <tag>, ...
  pose: <tag>, <tag>, ...
  expression: <tag>, <tag>, ...
  setting: <tag>, <tag>, ...
  style: <tag>, <tag>, ...
  quality: <tag>, <tag>, ...

Concepts with no tags are simply OMITTED — do not write empty lines or placeholders. No commentary, no markdown fences.

COMPLETENESS:
Every input token belongs to exactly ONE concept output line. Don't drop tokens you don't recognise — pick the most plausible concept:
  - any `*_focus` token → pose
  - any `from_*` / `*_angle` viewpoint token → pose
  - bare color words with no item → outfit
  - generic ambient/atmosphere words → setting

STRICT NO-REPEAT:
Emit each concept line AT MOST ONCE. Never re-emit a line you've already written. Never echo earlier output. Never write a verification or summary block after the labelled lines. As soon as you've placed every input token once, stop.

CRITICAL DISTINCTION — character vs outfit:
The character line holds the SUBJECT and PERMANENT BODY TRAITS only: the named character token, subject count (`1girl`/`2girls`/`1boy`), hair color/length/style, eye color, skin tone, body build, breast/bust size, scars, freckles. CLOTHING NEVER goes on the character line — any garment, footwear, headwear, glove, accessory, or worn-state modifier goes on the outfit line, not the character line."""


_TAG_SCAN_CONCEPTS = (
    "character", "outfit", "pose", "expression",
    "setting", "style", "quality",
)


def _parse_tag_scan(raw: str) -> dict[str, list[str]]:
    """Parse `concept: tag1, tag2` lines into {concept: [tokens]}. Missing
    concepts map to empty list. Dedupes within each concept AND across
    concepts (first occurrence wins) so a runaway-repetition LLM stream
    can't bloat downstream sections."""
    import re as _re
    out: dict[str, list[str]] = {c: [] for c in _TAG_SCAN_CONCEPTS}
    text = (raw or "").strip()
    if not text:
        return out
    if text.startswith("```"):
        ls = text.splitlines()
        if ls and ls[0].startswith("```"):
            ls = ls[1:]
        if ls and ls[-1].startswith("```"):
            ls = ls[:-1]
        text = "\n".join(ls).strip()
    seen_global: set[str] = set()
    for line in text.splitlines():
        m = _re.match(r"^\s*([A-Za-z]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        concept = m.group(1).strip().lower()
        if concept not in _TAG_SCAN_CONCEPTS:
            continue
        for raw_t in m.group(2).split(","):
            t = raw_t.strip()
            if not t:
                continue
            key = t.lower()
            if key in seen_global:
                continue
            seen_global.add(key)
            out[concept].append(t)
    return out


async def _classify_flat_blob(
    blob: str, request_id: str, provider: str, config: dict,
) -> dict[str, list[str]]:
    """LLM-classify a flat comma-separated tag blob into concept buckets.
    Lets rails operate on headerless user prompts (someone pasting their
    own tags with no // Section markup) the same way it operates on
    rails-emitted prompts. Returns empty buckets on failure or anomalous
    output (caller should treat that as 'no classification, leave the
    blob alone')."""
    if not (blob or "").strip():
        return {c: [] for c in _TAG_SCAN_CONCEPTS}
    try:
        raw = await _api._run_generation(
            f"{request_id}-tagscan", provider, config,
            TAG_SCAN_SYSTEM, f"Tags to classify:\n{blob}", [],
        )
    except Exception:
        logger.exception("tag-rails[%s] tag-scan call failed", request_id)
        return {c: [] for c in _TAG_SCAN_CONCEPTS}
    # Anomaly guard: if the model went into a token-repetition loop
    # (observed: 541-char input → 45469-char output over 2:46), the
    # output is unusable. Bail rather than feed bloated sections to
    # downstream rewrite + coherence stages.
    if len(raw or "") > max(2000, 6 * len(blob)):
        logger.warning(
            "tag-rails[%s] tag-scan output anomalously large "
            "(raw=%d chars vs input=%d) — likely model loop, dropping classification",
            request_id, len(raw or ""), len(blob),
        )
        return {c: [] for c in _TAG_SCAN_CONCEPTS}
    parsed = _parse_tag_scan(raw)
    classified_count = sum(len(v) for v in parsed.values())
    logger.info(
        "tag-rails[%s] tag-scan classified %d tokens into %d concepts (input chars=%d)",
        request_id, classified_count,
        sum(1 for v in parsed.values() if v), len(blob),
    )
    return parsed


def _extract_character_canon_from_tokens(
    tokens: list[str], bios: list[dict],
) -> str:
    """Find a known character canon among the classified character tokens.
    Tries: (a) `(name:weight)` parenthesised form, (b) exact token match
    against bio.tag for any bio. Returns empty string if no match."""
    import re as _re
    bio_tags = {(b.get("tag") or "").lower() for b in (bios or []) if b}
    for t in tokens:
        m = _re.match(r"^\(?([a-z][a-z0-9_\\\(\)]+?)(?::[0-9.]+)?\)?$", t.lower())
        candidate = m.group(1) if m else t.lower()
        candidate = candidate.replace("\\(", "(").replace("\\)", ")").replace(" ", "_")
        if candidate in bio_tags:
            return candidate
    return ""


def _virtual_sections_from_classified(
    classified: dict[str, list[str]], bios: list[dict],
) -> list[dict]:
    """Build [{header, tokens, body_text, is_negative}] virtual sections
    from classifier output, mirroring `_parse_sectioned_output` shape so
    the rest of compose runs unchanged. Order matches the standard
    rails section order."""
    out: list[dict] = []
    char_tokens = classified.get("character") or []
    if char_tokens:
        canon = _extract_character_canon_from_tokens(char_tokens, bios)
        header = f"// Character: {canon}" if canon else "// Character"
        out.append({"header": header, "tokens": list(char_tokens),
                    "body_text": "", "is_negative": False})
    outfit_tokens = classified.get("outfit") or []
    if outfit_tokens:
        out.append({"header": "// Outfit", "tokens": list(outfit_tokens),
                    "body_text": "", "is_negative": False})
    pose_tokens = classified.get("pose") or []
    if pose_tokens:
        out.append({"header": "// Pose, Action & Prop",
                    "tokens": list(pose_tokens),
                    "body_text": "", "is_negative": False})
    expr_tokens = classified.get("expression") or []
    if expr_tokens:
        out.append({"header": "// Expression", "tokens": list(expr_tokens),
                    "body_text": "", "is_negative": False})
    setting_tokens = classified.get("setting") or []
    if setting_tokens:
        out.append({"header": "// Setting", "tokens": list(setting_tokens),
                    "body_text": "", "is_negative": False})
    style_tokens = classified.get("style") or []
    if style_tokens:
        out.append({"header": "// Style", "tokens": list(style_tokens),
                    "body_text": "", "is_negative": False})
    quality_tokens = classified.get("quality") or []
    if quality_tokens:
        out.append({"header": "// Quality", "tokens": list(quality_tokens),
                    "body_text": "", "is_negative": False})
    return out


async def _llm_rewrite_section(
    section_header: str,
    current_tokens: list[str],
    intent: dict,
    candidates: list[str],
    request_id: str,
    provider: str,
    config: dict,
) -> Optional[list[str]]:
    """Narrow LLM call: rewrite ONE section's tokens given one intent
    plus candidate canonical tags. Returns the new token list, or None
    if the LLM said UNCHANGED or the call failed."""
    intent_action = (intent.get("action") or "").lower()
    intent_text = (intent.get("text") or "").strip()
    if not intent_text:
        return None

    # Action semantics make-or-break the rewrite. Be explicit:
    #   add / None → APPEND (keep current tokens, add new ones)
    #   remove     → drop matching tokens from current
    #   replace / swap / fill → REPLACE the whole list with new tokens
    #                            (user is fresh-setting this section)
    #   modify     → mutate existing tokens (color swap, qualifier add)
    if intent_action in ("replace", "swap", "fill"):
        action_hint = (
            "INTENT (action=REPLACE — discard ALL current tokens "
            "and output ONLY the tokens that satisfy this intent)"
        )
    elif intent_action == "remove":
        action_hint = (
            "INTENT (action=REMOVE — drop tokens matching this from "
            "the current list, keep the rest)"
        )
    elif intent_action == "modify":
        action_hint = (
            "INTENT (action=MODIFY — mutate existing tokens to apply "
            "this change; e.g. color swap, add qualifier in place)"
        )
    else:
        # add / None → append; preserve existing tokens unless they
        # directly conflict (e.g. red_socks displaces barefoot)
        action_hint = (
            "INTENT (action=ADD — keep ALL current tokens, append new "
            "ones for this intent; drop only tokens that directly "
            "conflict with what you're adding)"
        )

    current_str = ", ".join(current_tokens) if current_tokens else "(empty)"
    candidates_str = ", ".join(candidates[:20]) if candidates else "(none)"

    user_msg = (
        f"SECTION: {section_header}\n"
        f"CURRENT TOKENS: {current_str}\n"
        f"{action_hint}: {intent_text}\n"
        f"CANDIDATES: {candidates_str}\n\n"
        f"Updated tokens:"
    )

    try:
        raw = await _api._run_generation(
            f"{request_id}-rewrite",
            provider, config,
            TAG_SECTION_REWRITE_SYSTEM, user_msg, [],
        )
    except Exception:
        logger.exception("tag-rails: LLM rewrite call failed")
        return None
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw or raw.upper() == "UNCHANGED":
        return None

    # Take only the first non-empty line — model sometimes adds extra
    new_tokens: list[str] = []
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        # Drop header-like leaks
        if s.startswith("//"):
            continue
        for tok in s.split(","):
            t = tok.strip()
            if not t:
                continue
            # Strip stray quotes / parens balance is fine
            if t.startswith('"') and t.endswith('"'):
                t = t[1:-1].strip()
            if t and not t.startswith("//"):
                new_tokens.append(t)
        if new_tokens:
            break
    return new_tokens if new_tokens else None


# ── public entry point ──────────────────────────────────────────────


async def run_tag_rails(
    node_prompt: str,
    user_request: str,
    bios: list[dict],
    *,
    model_hash: str = "",
    provider: str = PROVIDER_DEFAULT,
    config: dict | None = None,
    request_id: str = "tag-rails",
    tag_format: str = "spaces",
    is_standalone_main: bool = True,
) -> dict:
    """Single tag-rails turn.

    Returns:
      {
        "final_prompt": str,       # assembled output_text
        "sections":     list[dict], # parsed section list
        "intents":      list[dict], # decomposed sub_intents
        "trace":        list[str],  # diagnostic notes
      }
    """
    config = config or CONFIG_DEFAULT
    trace: list[str] = []

    is_build = not (node_prompt or "").strip()
    trace.append(f"mode={'build' if is_build else 'patch'} bios={len(bios)}")

    # ── Stage 1: decompose ──────────────────────────────────────────
    try:
        sub_intents = await _api._decompose_user_request(
            request_id, provider, config, user_request,
        )
    except Exception as e:
        logger.error("tag-rails: decompose failed: %s", e, exc_info=True)
        sub_intents = []
    sub_intents = sub_intents or []
    trace.append(f"decompose: {len(sub_intents)} intents")

    # ── Stage 2: resolve ────────────────────────────────────────────
    # Per-intent canonical lookup. For now we use the existing
    # canonical_resolver and tag_search infrastructure as-is — they
    # already give per-intent canonical tags. Resolution attaches a
    # `_resolved_tags` field to each intent.
    resolved_intents = await _resolve_intents(
        sub_intents, bios, user_request, request_id, provider, config,
    )
    n_resolved = sum(1 for it in resolved_intents
                      if it.get("_resolved_tags"))
    trace.append(f"resolve: {n_resolved}/{len(resolved_intents)} intents resolved")
    # Per-intent visibility so dev-log diagnosis matches the harness
    for i, it in enumerate(resolved_intents):
        try:
            _api.dbg.info(
                "tag-rails[%s] intent[%d] section=%r action=%r text=%r resolved=%r",
                request_id, i,
                it.get("section"), it.get("action"),
                it.get("text"), it.get("_resolved_tags"),
            )
        except Exception:
            pass

    # ── Stage 3: compose ────────────────────────────────────────────
    # Build mode: deterministic (build from bio).
    # Patch mode: per-section narrow LLM rewrite (natlang's fragment-
    # rewrite pattern) for any section with targeted intents.
    if is_build:
        sections = await _compose_build_mode(
            resolved_intents, bios, model_hash, is_standalone_main,
            request_id, provider, config,
        )
    else:
        sections = await _compose_patch_mode(
            resolved_intents, bios, node_prompt, model_hash,
            request_id, provider, config,
        )
    trace.append(f"compose: {len(sections)} sections")

    # ── Stage 4: coherence (deterministic) ──────────────────────────
    intent_added = _collect_intent_added_tokens(resolved_intents)
    # An outfit intent (any kind) means the LLM rewrote // Outfit;
    # coherence's bio-driven refresh must not clobber that.
    outfit_was_rewritten = any(
        (it.get("section") or "").lower() in ("outfit", "strip")
        for it in resolved_intents
    )
    sections = _apply_coherence(
        sections, bios, user_request, node_prompt, model_hash, request_id,
        intent_added_tokens=intent_added,
        outfit_was_rewritten=outfit_was_rewritten,
    )

    # ── assemble output text ────────────────────────────────────────
    output_text = _assemble_output_text(sections, tag_format=tag_format)
    trace.append(f"output: {len(output_text)} chars, {len(sections)} sections")

    # Dump the assembled output for debugging — mirrors the legacy
    # patch_raw block so a comparison-by-eyeball is straightforward.
    try:
        _api.dbg.info(
            "tag-rails[%s] output (%d chars, %d sections):\n%s\n--- /tag-rails-output ---",
            request_id, len(output_text), len(sections), output_text,
        )
    except Exception:
        pass

    return {
        "final_prompt": output_text,
        "sections": sections,
        "intents": resolved_intents,
        "trace": trace,
    }


# ── Stage 2: resolve ────────────────────────────────────────────────


async def _resolve_intents(
    sub_intents: list[dict], bios: list[dict],
    user_request: str, request_id: str,
    provider: str, config: dict,
) -> list[dict]:
    """Attach canonical tags to each intent. For now this delegates
    to the existing canonical_resolver + tag_search retrieval. Bio-
    backed intents (character/outfit/pose names matching a curated
    DB row) get their tags from bio data directly."""
    out: list[dict] = []
    for it in sub_intents:
        intent = dict(it)
        section = (it.get("section") or "").lower()
        text = (it.get("text") or "").strip()
        intent["_resolved_tags"] = []

        if not text:
            out.append(intent)
            continue

        # Bio-backed lookups for character/outfit/pose
        if section == "character":
            # Find the matching bio; its tag IS the canonical
            bio = _find_bio_by_phrase(text, bios)
            if bio:
                intent["_resolved_tags"] = [bio.get("tag") or text]
                intent["_bio"] = bio
                out.append(intent)
                continue

        if section == "pose":
            # Bio.matched_pose carries curated pose tags from the
            # character DB (e.g. Cammy's 'Victory Pose (Rear)' →
            # from_behind, looking_back, hand_on_hip). If the user
            # named a pose that matches the bio's curated pose,
            # use those tags verbatim — bio is authoritative, skip
            # both LLM resolve AND LLM rewrite.
            bio_pose_tags = _bio_pose_tags_for_text(text, bios)
            if bio_pose_tags:
                intent["_resolved_tags"] = bio_pose_tags
                intent["_bio_authoritative"] = True
                out.append(intent)
                continue

        if section == "outfit":
            # Bio.user_requested_outfit means the picker already
            # selected the right outfit by name. Compose stage's
            # _build_outfit_section will emit the slot tokens
            # directly — the intent's literal outfit-name token
            # would be a duplicate. Suppress the literal fallback.
            if _bio_outfit_name_matches_intent(text, bios):
                intent["_resolved_tags"] = []
                intent["_suppress_literal_fallback"] = True
                out.append(intent)
                continue

        # canonical_resolver for everything else
        try:
            from core import canonical_resolver as _cr
            tags = await _cr.resolve(
                provider, config, text, section=section,
                request_id=f"{request_id}-resolve",
            )
        except Exception:
            tags = []
        if tags:
            intent["_resolved_tags"] = list(tags)
        out.append(intent)
    return out


def _bio_pose_tags_for_text(text: str, bios: list[dict]) -> list[str]:
    """If bio.matched_pose's name fuzzy-matches the intent text, return
    the curated pose tags. e.g. user says 'victory pose' and bio's
    matched_pose is 'Victory Pose (Rear)' with tags from_behind,
    looking_back, ... — return those."""
    t_norm = (text or "").lower().replace("_", " ").replace("-", " ").strip()
    if not t_norm:
        return []
    for b in bios or []:
        if not b or b.get("_outfit_source_only"):
            continue
        pose = b.get("matched_pose") or {}
        pose_name = (pose.get("name") or "").lower()
        if not pose_name:
            continue
        pose_name_norm = pose_name.replace("_", " ").replace("-", " ")
        # Fuzzy match: intent text appears in pose name OR vice versa
        if (t_norm in pose_name_norm or pose_name_norm in t_norm
                or _shares_significant_word(t_norm, pose_name_norm)):
            tags_str = (pose.get("tags") or "").strip()
            if not tags_str:
                continue
            return [t.strip() for t in tags_str.split(",") if t.strip()]
    return []


def _shares_significant_word(a: str, b: str) -> bool:
    """At least one word ≥4 chars in common (case-insensitive)."""
    stopwords = {"pose", "from", "with", "the", "and", "for"}
    a_words = {w for w in a.split() if len(w) >= 4 and w not in stopwords}
    b_words = {w for w in b.split() if len(w) >= 4 and w not in stopwords}
    return bool(a_words & b_words)


def _bio_outfit_name_matches_intent(text: str, bios: list[dict]) -> bool:
    """True if any non-source bio's user_requested_outfit name matches
    the intent text. Means compose will use the outfit's slot tokens
    directly — the intent's literal token would be a duplicate."""
    t_norm = (text or "").lower().replace("_", " ").replace("-", " ").strip()
    if not t_norm:
        return False
    for b in bios or []:
        if not b or b.get("_outfit_source_only"):
            continue
        outfit = b.get("user_requested_outfit") or {}
        name = (outfit.get("name") or "").lower()
        if not name:
            continue
        name_norm = name.replace("_", " ").replace("-", " ")
        if (t_norm in name_norm or name_norm in t_norm
                or _shares_significant_word(t_norm, name_norm)):
            return True
    return False


def _find_bio_by_phrase(phrase: str, bios: list[dict]) -> Optional[dict]:
    p_norm = (phrase or "").lower().replace("_", " ").replace("-", " ").strip()
    if not p_norm:
        return None
    for b in bios or []:
        if not b or b.get("_outfit_source_only"):
            continue
        tag = (b.get("tag") or "").lower().replace("_", " ").replace("-", " ")
        display = (b.get("display") or "").lower()
        if p_norm == tag or p_norm == display:
            return b
        if len(p_norm) >= 3 and (p_norm in tag or p_norm in display):
            return b
    return None


# ── Stage 3: compose (build mode) ───────────────────────────────────


async def _compose_build_mode(
    intents: list[dict], bios: list[dict],
    model_hash: str, is_standalone_main: bool,
    request_id: str, provider: str, config: dict,
) -> list[dict]:
    """Build a fresh sectioned prompt. Two passes:
      1. Deterministic skeleton from bio (Character + Outfit + Style + Neg).
      2. Per-section LLM rewrite for any intents that target an
         emitted section. This is what makes 'pink micro bikini' or
         'spreading feet' actually become canonical tokens.
    """
    sections: list[dict] = []

    subject_bios = [b for b in bios if b and not b.get("_outfit_source_only")]
    source_bio = next(
        (b for b in bios if b and b.get("_outfit_source_only")), None,
    )

    by_section: dict[str, list[dict]] = defaultdict(list)
    for it in intents:
        by_section[(it.get("section") or "").lower()].append(it)

    # Character + Outfit sections — one per subject bio (skeleton)
    for bio in subject_bios:
        sections.append(_build_character_section(bio))
        outfit_section = _build_outfit_section(bio)
        if outfit_section:
            sections.append(outfit_section)

    # Outfit borrow: replace primary's outfit with source_bio's
    if source_bio and subject_bios:
        sections = _apply_outfit_borrow_to_sections(
            sections, source_bio, subject_bios[0],
        )

    # Pose / Expression / Setting / Quality skeletons (empty token
    # lists if user named them; LLM rewrite fills them in)
    pose_intents = (by_section.get("pose", []) +
                    by_section.get("action", []) +
                    by_section.get("prop", []))
    if pose_intents:
        sections.append({"header": "// Pose, Action & Prop",
                         "tokens": [], "body_text": "",
                         "is_negative": False})

    expr_intents = by_section.get("expression", [])
    if expr_intents:
        sections.append({"header": "// Expression", "tokens": [],
                         "body_text": "", "is_negative": False})

    setting_intents = (by_section.get("setting", []) +
                       by_section.get("scene", []))
    if setting_intents:
        sections.append({"header": "// Setting / Scene", "tokens": [],
                         "body_text": "", "is_negative": False})

    style_intents = by_section.get("style", [])
    style_section = _build_style_section(
        style_intents, model_hash, is_standalone_main,
    )
    if style_section:
        sections.append(style_section)

    quality_intents = by_section.get("quality", [])
    if quality_intents:
        sections.append({"header": "// Quality", "tokens": [],
                         "body_text": "", "is_negative": False})

    # Negative Prompt — always emitted (server populates it)
    neg = _build_negative_section(bios, sections, model_hash)
    sections.append(neg)

    # Pass 2: LLM rewrite each section that has matching intents.
    # Character section uses the bio's base_tags as-is (no intent
    # rewrite — character is fully bio-driven). Outfit gets a rewrite
    # if the user named a specific outfit. Pose/Setting/etc. get
    # populated by the rewrite from their (currently empty) skeletons.
    by_concept: dict[str, list[dict]] = defaultdict(list)
    for it in intents:
        c = _intent_section_concept(it)
        if c:
            by_concept[c].append(it)

    rewritten: list[dict] = []
    for s in sections:
        if s.get("is_negative"):
            rewritten.append(s)
            continue
        concept = _section_concept_from_header(s.get("header") or "")
        if not concept:
            rewritten.append(s)
            continue
        # Skip character — bio is authoritative
        if concept == "character":
            rewritten.append(s)
            continue
        # Skip style — server-managed (auto-seed)
        if concept == "style":
            rewritten.append(s)
            continue
        section_intents = by_concept.get(concept, [])
        if not section_intents:
            rewritten.append(s)
            continue
        new_s = await _rewrite_section_with_intents(
            dict(s), section_intents, bios,
            request_id, provider, config,
        )
        rewritten.append(new_s)

    return rewritten


def _collect_intent_added_tokens(intents: list[dict]) -> set[str]:
    """Set of canonical-form tokens the user's intents are adding this
    turn. Used to override Negative-section carry-forward: when the
    user says `wearing red socks` and `red_socks` is stuck in Negative
    from a prior barefoot turn, the user's new positive must win —
    legacy `_dedupe_negatives_from_positives` does the opposite ('neg
    wins'), so rails strips intent-added tokens from negatives BEFORE
    that dedupe runs."""
    added: set[str] = set()
    for it in intents:
        if (it.get("action") or "").lower() == "remove":
            continue
        for tag in _intent_canonical_tags(it):
            c = _canon_compare(tag)
            if c:
                added.add(c)
    return added


def _strip_intent_tokens_from_negatives(
    sections: list[dict], intent_added: set[str], request_id: str,
) -> list[dict]:
    if not intent_added:
        return sections
    dropped: list[str] = []
    for s in sections:
        if not s.get("is_negative"):
            continue
        kept = []
        for t in s.get("tokens") or []:
            if _canon_compare(t) in intent_added:
                dropped.append(t)
                continue
            kept.append(t)
        s["tokens"] = kept
    if dropped:
        logger.info(
            "tag-rails[%s] stripped from negative (intent-added positives win): %s",
            request_id, ", ".join(dropped),
        )
    return sections


async def _compose_patch_mode(
    intents: list[dict], bios: list[dict],
    node_prompt: str, model_hash: str,
    request_id: str, provider: str, config: dict,
) -> list[dict]:
    """Patch existing node_prompt with deltas from intents. Untouched
    sections survive verbatim — no LLM call for them. Sections with
    targeted intents go through a narrow per-section LLM rewrite
    (natlang's fragment-rewrite pattern).

    Strategy:
      1. Parse node_prompt into sections
      2. Group intents by section concept
      3. For each existing section with intents: narrow LLM rewrite
         (single section + intent + retrieval candidates -> new tokens)
      4. Append new sections for intent concepts that don't have an
         existing section yet
      5. Outfit borrow: if a bio is _outfit_source_only, rewrite the
         primary // Outfit section with the source's slots + header.
    """
    existing = _api._parse_sectioned_output(node_prompt) or []

    # Headerless rescue. If the user pasted a flat tag list with no
    # `// Section:` markup, the parser dumps everything into a synthetic
    # `// Prompt` section that downstream concept routing can't read.
    # Run a narrow LLM classifier to bucket those tokens into typed
    # concept sections so the rest of compose operates as if rails had
    # authored the prompt.
    raw_blob_section = next(
        (s for s in existing
         if (s.get("header") or "") == "// Prompt"
         and not s.get("is_negative")),
        None,
    )
    has_rails_headers = any(
        _section_concept_from_header(s.get("header") or "") is not None
        and not s.get("is_negative")
        for s in existing
    )
    if raw_blob_section and not has_rails_headers:
        blob = ", ".join(raw_blob_section.get("tokens") or [])
        classified = await _classify_flat_blob(
            blob, request_id, provider, config,
        )
        virtual = _virtual_sections_from_classified(classified, bios)
        if virtual:
            existing = [
                *virtual,
                *(s for s in existing if s is not raw_blob_section),
            ]
            logger.info(
                "tag-rails[%s] headerless: classifier emitted %d sections "
                "(%s)", request_id, len(virtual),
                ", ".join((s.get("header") or "").strip() for s in virtual),
            )

    source_bio = next(
        (b for b in (bios or []) if b and b.get("_outfit_source_only")),
        None,
    )

    by_concept: dict[str, list[dict]] = defaultdict(list)
    for it in intents:
        c = _intent_section_concept(it)
        if c:
            by_concept[c].append(it)

    # Per-canon swap delta. Bios is treated as the FINAL CAST: the set
    # of characters the user wants in the output. Compute:
    #   displaced = existing_canons - new_canons  (drop these)
    #   added     = new_canons - existing_canons  (build sections for)
    #   preserved = existing_canons & new_canons  (leave untouched)
    #
    # Single-char swap is the same shape with 1-element sets.
    # Multi-char "change chun-li to ryu" with cammy in scene:
    #   existing={cammy, chun-li}, new={cammy, ryu}
    #   → drop chun-li sections, build ryu sections, keep cammy.
    subject_bios = [b for b in bios if b and not b.get("_outfit_source_only")]
    new_bio_canons = {(b.get("tag") or "").lower() for b in subject_bios}
    bios_by_canon = {(b.get("tag") or "").lower(): b for b in subject_bios}

    # Walk existing sections and assign each to its owning character
    # canon. // Character heads a new chunk; immediately-following
    # // Outfit is owned by that character. Other concept sections
    # (pose / setting / expression / style / quality) and negatives are
    # global (owner=None).
    #
    # Canon extraction prefers the header (`// Character: cammy_white`)
    # but falls back to scanning tokens for `(name:weight)` — needed
    # for headerless virtual sections where the classifier emits
    # `// Character` (no canon) but the tokens still carry the
    # weighted-name marker.
    import re as _re_canon

    def _canon_from_section(s: dict) -> str:
        header_lc = (s.get("header") or "").lower()
        if ":" in header_lc and header_lc.startswith("// character"):
            after_colon = header_lc.split(":", 1)[1].strip().split("(", 1)[0].strip()
            if after_colon:
                return after_colon.replace(" ", "_")
        for t in s.get("tokens") or []:
            t_norm = (t or "").lower().strip()
            m = _re_canon.match(
                r"^\(?([a-z][a-z0-9_\-\\\(\)\.]+?)(?::[0-9.]+)?\)?$",
                t_norm,
            )
            if m:
                candidate = (
                    m.group(1).replace("\\(", "(").replace("\\)", ")")
                    .replace(" ", "_")
                )
                if "_" in candidate or "(" in candidate or "-" in candidate:
                    return candidate
        return ""

    # Sentinel owner string for character sections whose canon can't
    # be identified (anonymous character — e.g. a CivitAI prompt with
    # generic body tokens but no named character). Treated as a
    # candidate displacement when the user adds any new bio: the
    # natural read of "change character to X" with an anonymous
    # character in the scene is "replace whoever is depicted", not
    # "add a second person".
    UNNAMED = "__unnamed__"

    section_owners: list[Optional[str]] = []
    current_owner: Optional[str] = None
    existing_char_canons: list[str] = []  # preserve order, named only
    has_unnamed_char_section = False
    for s in existing:
        header_lc = (s.get("header") or "").lower()
        if header_lc.startswith("// character") and not s.get("is_negative"):
            canon = _canon_from_section(s)
            if canon:
                current_owner = canon
                if canon not in existing_char_canons:
                    existing_char_canons.append(canon)
            else:
                current_owner = UNNAMED
                has_unnamed_char_section = True
            section_owners.append(current_owner)
        elif header_lc.startswith("// outfit") and not s.get("is_negative"):
            section_owners.append(current_owner)
        else:
            # global section — closes any open char chunk
            current_owner = None
            section_owners.append(None)

    existing_canon_set = set(existing_char_canons)
    displaced = existing_canon_set - new_bio_canons
    added = new_bio_canons - existing_canon_set

    # Anonymous-character displacement: if existing has a // Character
    # section we couldn't identify (e.g. CivitAI prompt with generic
    # body tokens, no named char) AND the user is adding any new bio,
    # treat that unnamed section as displaced too. The natural read of
    # "change character to X" with an anonymous character in the scene
    # is replace, not stack.
    if has_unnamed_char_section and added:
        displaced = displaced | {UNNAMED}

    swap_prepended: list[dict] = []

    if displaced or added:
        user_asked_outfit_change_global = (
            bool(by_concept.get("outfit"))
            or bool(by_concept.get("strip"))
        )

        # Single-char swap heuristic: exactly one char displaced and
        # exactly one char added with no preserved chars (the
        # "change character to X" case the user expects to keep
        # their scene). In that case we preserve the displaced
        # character's outfit as the new char's outfit, unless the
        # user requested an outfit change.
        preserved_overlap = existing_canon_set & new_bio_canons
        is_single_swap = (
            len(displaced) == 1 and len(added) == 1
            and not preserved_overlap
        )
        sole_added_bio = (
            bios_by_canon.get(next(iter(added))) if is_single_swap else None
        )
        user_requested_outfit_for_swap = bool(
            sole_added_bio and sole_added_bio.get("user_requested_outfit")
        )
        preserve_outfit_on_single_swap = is_single_swap and not (
            user_requested_outfit_for_swap or user_asked_outfit_change_global
        )

        # Filter existing: drop displaced // Character always.
        # Drop displaced // Outfit unless single-swap preservation is
        # in effect (then keep — it becomes the new char's outfit).
        filtered: list[dict] = []
        for s, owner in zip(existing, section_owners):
            if owner is not None and owner in displaced:
                header_lc = (s.get("header") or "").lower()
                if header_lc.startswith("// character"):
                    continue
                if header_lc.startswith("// outfit"):
                    if preserve_outfit_on_single_swap:
                        filtered.append(s)
                    continue
            filtered.append(s)
        existing = filtered

        # Build sections for added canons in subject_bios order. Skip
        # // Outfit when single-swap preserved the displaced outfit
        # (it's already in `existing` and will pass through).
        for bio in subject_bios:
            canon = (bio.get("tag") or "").lower()
            if canon not in added:
                continue
            swap_prepended.append(_build_character_section(bio))
            if preserve_outfit_on_single_swap:
                continue
            o = _build_outfit_section(bio)
            if o:
                swap_prepended.append(o)
            else:
                logger.warning(
                    "tag-rails[%s] swap: _build_outfit_section None for "
                    "bio.tag=%s default_outfit=%s user_req=%s slots=%d",
                    request_id, bio.get("tag"),
                    (bio.get("default_outfit") or {}).get("name"),
                    (bio.get("user_requested_outfit") or {}).get("name"),
                    len((bio.get("default_outfit") or {}).get("slots") or []),
                )

        # Character intent has been consumed by the delta above —
        # don't run it through per-section rewrite (which would
        # otherwise inject ryu's tokens into cammy's section).
        by_concept.pop("character", None)

        # If the delta changed the cast size, scrub stale aggregate-
        # count tokens (2girls/2boys/3girls/etc.) from any section.
        # _enforce_multi_char_composition only scrubs BREAK in single-
        # char mode; we need to drop the multi-count tokens too when
        # going from 2girls scene to 1girl scene.
        final_count = len(subject_bios)
        stale_aggregate_tokens = {
            t for t in (
                "2girls", "2boys", "3girls", "3boys",
                "4girls", "4boys", "multiple_girls", "multiple_boys",
            )
            if not (
                (final_count >= 2 and t == "2girls")
                or (final_count >= 2 and t == "2boys")
                # ... narrow case: above 2 stays, but we keep this
                # simple — multi_char_composition will inject the
                # correct aggregate downstream.
            )
        }
        scrubbed_count = 0
        for s in existing:
            tokens = s.get("tokens") or []
            kept = [t for t in tokens
                    if t.strip().lower() not in stale_aggregate_tokens]
            if len(kept) != len(tokens):
                scrubbed_count += len(tokens) - len(kept)
                s["tokens"] = kept

        logger.info(
            "tag-rails[%s] per-canon delta: displaced=%s added=%s "
            "preserved=%s%s%s",
            request_id, sorted(displaced), sorted(added),
            sorted(preserved_overlap),
            " (outfit preserved)" if preserve_outfit_on_single_swap else "",
            f" scrubbed {scrubbed_count} stale count tokens"
            if scrubbed_count else "",
        )

    out: list[dict] = list(swap_prepended)
    seen_concepts: set[str] = set()
    if swap_prepended:
        seen_concepts.add("character")
        if any((s.get("header") or "").lower().startswith("// outfit")
               for s in swap_prepended):
            seen_concepts.add("outfit")
    for s in existing:
        header_concept = _section_concept_from_header(s.get("header") or "")
        if not header_concept:
            out.append(s)
            continue
        section_intents = by_concept.get(header_concept, [])
        if section_intents:
            s = await _rewrite_section_with_intents(
                dict(s), section_intents, bios,
                request_id, provider, config,
            )
        out.append(s)
        seen_concepts.add(header_concept)

    # Concepts with intents but no existing section — insert into
    # positive area (before the first Negative Prompt: section) so
    # the assembled output keeps `positive ... \n Negative Prompt: ...`
    # structure. Without this split, new outfit/pose etc. sections
    # would land AFTER Negative and be compiled as negative content.
    def _insert_positive(section: dict) -> None:
        for i, s in enumerate(out):
            if s.get("is_negative"):
                out.insert(i, section)
                return
        out.append(section)

    for concept, intent_list in by_concept.items():
        if concept in seen_concepts:
            continue
        if concept == "character":
            for bio in bios:
                if bio and not bio.get("_outfit_source_only"):
                    _insert_positive(_build_character_section(bio))
                    o = _build_outfit_section(bio)
                    if o:
                        _insert_positive(o)
                    else:
                        logger.warning(
                            "tag-rails[%s] _build_outfit_section returned None for "
                            "bio.tag=%s default_outfit=%s user_req=%s slots=%d tags_blob=%r",
                            request_id, bio.get("tag"),
                            (bio.get("default_outfit") or {}).get("name"),
                            (bio.get("user_requested_outfit") or {}).get("name"),
                            len((bio.get("default_outfit") or {}).get("slots") or []),
                            ((bio.get("default_outfit") or {}).get("tags") or "")[:80],
                        )
                    seen_concepts.add(concept)
                    seen_concepts.add("outfit")
            continue
        if concept == "outfit":
            for bio in bios:
                if bio and not bio.get("_outfit_source_only"):
                    o = _build_outfit_section(bio)
                    if o:
                        _insert_positive(o)
        elif concept == "pose":
            _insert_positive(_build_pose_section(intent_list))
        elif concept == "expression":
            _insert_positive(_build_expression_section(intent_list))
        elif concept == "setting":
            _insert_positive(_build_setting_section(intent_list))
        elif concept == "style":
            ss = _build_style_section(intent_list, model_hash, False)
            if ss:
                _insert_positive(ss)
        elif concept == "quality":
            _insert_positive(_build_quality_section(intent_list))

    # Outfit borrow: rewrite // Outfit with source bio's slots + header
    if source_bio:
        primary_bio = next(
            (b for b in bios if b and not b.get("_outfit_source_only")),
            None,
        )
        if primary_bio:
            out = _apply_outfit_borrow_to_sections(out, source_bio, primary_bio)

    return out


_HEADER_CONCEPT_MAP = {
    "character": "character",
    "outfit":    "outfit",
    "pose":      "pose",
    "action":    "pose",
    "prop":      "pose",
    "expression": "expression",
    "setting":   "setting",
    "scene":     "setting",
    "style":     "style",
    "quality":   "quality",
}


def _section_concept_from_header(header: str) -> Optional[str]:
    h = (header or "").lstrip("/").strip().lower()
    if not h:
        return None
    first = h.split(":", 1)[0].split(",", 1)[0].split()[0] if h.split() else ""
    return _HEADER_CONCEPT_MAP.get(first)


_INTENT_SECTION_MAP = {
    "character": "character", "subject": "character",
    "outfit":    "outfit",    "strip": "outfit",
    "pose":      "pose",      "action": "pose", "prop": "pose",
    "expression": "expression",
    "setting":   "setting",   "scene": "setting",
    "style":     "style",
    "quality":   "quality",
}


def _intent_section_concept(intent: dict) -> Optional[str]:
    return _INTENT_SECTION_MAP.get((intent.get("section") or "").lower())


async def _rewrite_section_with_intents(
    section: dict, intents: list[dict], bios: list[dict],
    request_id: str, provider: str, config: dict,
) -> dict:
    """Apply intents to a section via narrow per-intent LLM rewrite
    (natlang's fragment-rewrite pattern). Each intent gets one LLM
    call with: section + intent text + retrieval candidates. The LLM
    handles add/remove/replace/modify/expand semantics naturally.

    Falls back to deterministic `_apply_intents_to_section` if the
    LLM returns UNCHANGED or fails."""
    current_tokens = list(section.get("tokens") or [])
    header = section.get("header") or ""

    # Outfit-borrow case: the header carries 'from Character: <X>'
    # and the body has already been authoritatively populated with
    # the source bio's slot data by _apply_outfit_borrow_to_sections.
    # The LLM rewrite would rewrite the body again with no useful
    # input — skip outfit intents entirely.
    if "from character:" in header.lower():
        return section

    for intent in intents:
        # Skip the LLM call when bio already authoritatively handled
        # this intent (named outfit / curated pose match). Use the
        # bio's resolved tags directly — they're already canonical.
        if intent.get("_suppress_literal_fallback"):
            continue
        if intent.get("_bio_authoritative"):
            stub = dict(section)
            stub["tokens"] = current_tokens
            stub = _apply_intents_to_section(stub, [intent])
            current_tokens = stub.get("tokens") or current_tokens
            logger.info(
                "tag-rails[%s] section-rewrite (bio-authoritative) "
                "header=%r intent=%r resolved=%d",
                request_id, header, intent.get("text"),
                len(intent.get("_resolved_tags") or []),
            )
            continue
        # Build candidate list: bio outfit/pose tokens + canonical
        # resolver + bge tag_search results for this intent
        candidates = _collect_candidates_for_intent(intent, bios)
        new_tokens = await _llm_rewrite_section(
            header, current_tokens, intent, candidates,
            request_id, provider, config,
        )
        logger.info(
            "tag-rails[%s] section-rewrite header=%r intent=%r "
            "action=%r before=%d after=%s",
            request_id, header, intent.get("text"),
            intent.get("action"), len(current_tokens),
            "UNCHANGED" if new_tokens is None else len(new_tokens),
        )
        if new_tokens is None:
            # LLM returned UNCHANGED or failed — fall back to
            # deterministic add/remove/replace using intent action
            stub = dict(section)
            stub["tokens"] = current_tokens
            stub = _apply_intents_to_section(stub, [intent])
            current_tokens = stub.get("tokens") or current_tokens
        else:
            current_tokens = new_tokens

    section["tokens"] = current_tokens
    return section


def _collect_candidates_for_intent(intent: dict, bios: list[dict]) -> list[str]:
    """Aggregate canonical-tag candidates for an intent — resolver
    output + bio-relevant tokens + (later) bge tag_search results."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in intent.get("_resolved_tags") or []:
        c = (tag or "").strip().lower()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    # Bio outfit slot tokens (for outfit-related intents)
    section = (intent.get("section") or "").lower()
    if section in ("outfit", "footwear", "legwear", "handwear",
                    "headwear", "neckwear", "accessories", "tops",
                    "bottoms", "dresses"):
        for b in bios or []:
            if not b:
                continue
            outfit = b.get("user_requested_outfit") or b.get("default_outfit") or {}
            for slot in outfit.get("slots") or []:
                phrase = (slot.get("source_phrase") or "").strip().lower()
                if phrase:
                    c = phrase.replace(" ", "_")
                    if c not in seen:
                        seen.add(c)
                        out.append(c)
    # Bio matched_pose tags (for pose intents)
    if section in ("pose", "action", "prop"):
        for b in bios or []:
            if not b:
                continue
            pose = b.get("matched_pose") or {}
            tags_str = (pose.get("tags") or "").strip().lower()
            for t in tags_str.split(","):
                t = t.strip()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def _apply_intents_to_section(
    section: dict, intents: list[dict],
) -> dict:
    """Apply add/remove/replace intents to a single section. Operates
    on a copy of the section's token list."""
    tokens = list(section.get("tokens") or [])
    seen_canon = {_canon_compare(t): i for i, t in enumerate(tokens)}

    for it in intents:
        action = (it.get("action") or "").lower()
        new_tags = _intent_canonical_tags(it)
        if action == "remove":
            for tag in new_tags:
                k = _canon_compare(tag)
                if k in seen_canon:
                    idx = seen_canon.pop(k)
                    tokens[idx] = None
            tokens = [t for t in tokens if t is not None]
            seen_canon = {_canon_compare(t): i for i, t in enumerate(tokens)}
        elif action == "replace":
            # Drop everything, add the new tags
            tokens = list(new_tags)
            seen_canon = {_canon_compare(t): i for i, t in enumerate(tokens)}
        else:
            # default: add
            for tag in new_tags:
                k = _canon_compare(tag)
                if k not in seen_canon:
                    seen_canon[k] = len(tokens)
                    tokens.append(tag)

    section["tokens"] = tokens
    return section


def _canon_compare(token: str) -> str:
    """Normalize a token for dedupe comparison: lowercase, swap
    underscores↔spaces, strip weight wrappers."""
    s = (token or "").strip().lower()
    if s.startswith("(") and ":" in s and s.endswith(")"):
        inner = s[1:-1]
        s = inner.rsplit(":", 1)[0].strip()
    return s.replace("_", " ").replace("-", " ")


# ── Section builders (deterministic) ────────────────────────────────


def _build_character_section(bio: dict) -> dict:
    """Build `// Character: <tag>` section from bio.base_tags."""
    tag = (bio.get("tag") or "").strip()
    header = f"// Character: {tag}" if tag else "// Character"
    tokens: list[str] = []
    base = (bio.get("base_tags") or "").strip()
    for raw in base.split(","):
        t = raw.strip()
        if t:
            tokens.append(t)
    return {
        "header": header, "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_outfit_section(bio: dict) -> Optional[dict]:
    """Build `// Outfit: <name>` from bio's user_requested_outfit or
    default_outfit. Prefers slot source_phrases, falls back to flat
    `outfit_tags` string."""
    outfit = (bio.get("user_requested_outfit")
              or bio.get("default_outfit") or {})
    if not outfit:
        return None
    name = (outfit.get("name") or "").strip()
    header = f"// Outfit: {name}" if name else "// Outfit"
    tokens = _api._bio_outfit_tokens(bio)  # reuse existing helper
    if not tokens:
        return None
    return {
        "header": header, "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_pose_section(intents: list[dict]) -> dict:
    tokens: list[str] = []
    seen: set[str] = set()
    for it in intents:
        for tag in _intent_canonical_tags(it):
            if tag and tag not in seen:
                seen.add(tag)
                tokens.append(tag)
    return {
        "header": "// Pose, Action & Prop",
        "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_expression_section(intents: list[dict]) -> dict:
    tokens: list[str] = []
    seen: set[str] = set()
    for it in intents:
        for tag in _intent_canonical_tags(it):
            if tag and tag not in seen:
                seen.add(tag)
                tokens.append(tag)
    return {
        "header": "// Expression",
        "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_setting_section(intents: list[dict]) -> dict:
    tokens: list[str] = []
    seen: set[str] = set()
    for it in intents:
        for tag in _intent_canonical_tags(it):
            if tag and tag not in seen:
                seen.add(tag)
                tokens.append(tag)
    return {
        "header": "// Setting / Scene",
        "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_quality_section(intents: list[dict]) -> dict:
    tokens: list[str] = []
    seen: set[str] = set()
    for it in intents:
        for tag in _intent_canonical_tags(it):
            if tag and tag not in seen:
                seen.add(tag)
                tokens.append(tag)
    return {
        "header": "// Quality",
        "tokens": tokens, "body_text": "",
        "is_negative": False,
    }


def _build_style_section(
    intents: list[dict], model_hash: str, is_standalone_main: bool,
) -> Optional[dict]:
    """Style: server-managed. If user named a style, use that. Else
    auto-seed from model's default_prompt_id (build mode + standalone
    main only)."""
    if intents:
        # User explicitly named a style — emit those tokens
        tokens: list[str] = []
        for it in intents:
            for tag in _intent_canonical_tags(it):
                if tag:
                    tokens.append(tag)
        if tokens:
            return {
                "header": "// Style",
                "tokens": tokens, "body_text": "",
                "is_negative": False,
            }

    if not is_standalone_main:
        return None

    # Auto-seed from model's default style
    try:
        grounding = _api._build_grounding(model_hash)
        default_id = grounding.get("default_prompt_id") or ""
        if not default_id:
            return None
        from core import prompts as _prompts
        tmpl = next(
            (p for p in _prompts.list_prompts() if p.get("id") == default_id),
            None,
        )
        if not tmpl:
            return None
        pos_mods, _neg = _api._parse_style_template_text(tmpl.get("text") or "")
        if not pos_mods:
            return None
        name = (tmpl.get("name") or "").strip()
        header = f"// Style: {name}" if name else "// Style"
        return {
            "header": header,
            "tokens": list(pos_mods),
            "body_text": "",
            "is_negative": False,
            "_template_id": default_id,
        }
    except Exception:
        logger.exception("tag-rails: style auto-seed failed")
        return None


def _build_negative_section(
    bios: list[dict], sections: list[dict], model_hash: str,
) -> dict:
    """Negative Prompt — server-managed. Starts with the model's
    style template negatives + any default-outfit phrases the active
    outfit displaced."""
    tokens: list[str] = []
    seen: set[str] = set()

    # 1. Style template negatives (if any style was auto-seeded)
    try:
        grounding = _api._build_grounding(model_hash)
        default_id = grounding.get("default_prompt_id") or ""
        if default_id:
            from core import prompts as _prompts
            tmpl = next(
                (p for p in _prompts.list_prompts()
                 if p.get("id") == default_id),
                None,
            )
            if tmpl:
                _pos, neg_mods = _api._parse_style_template_text(
                    tmpl.get("text") or "",
                )
                for t in neg_mods or []:
                    t_norm = t.strip()
                    if t_norm and t_norm.lower() not in seen:
                        seen.add(t_norm.lower())
                        tokens.append(t_norm)
    except Exception:
        pass

    # Header MUST be `Negative Prompt:` with trailing colon — A1111 /
    # SDXL pipeline parsers split the prompt body on this exact literal.
    # Without the colon, downstream treats the negative tokens as
    # positives. Bug surfaced in dev test 2026-05-19 22:24.
    return {
        "header": "Negative Prompt:",
        "tokens": tokens, "body_text": "",
        "is_negative": True,
    }


# ── Helpers ─────────────────────────────────────────────────────────


_LEADING_VERB_PREFIXES = (
    "wearing ", "with ", "has ", "in a ", "in an ", "in ",
    "shows ", "showing ", "having ", "make ", "give her ", "give him ",
    "add ", "include ",
)


def _intent_canonical_tags(intent: dict) -> list[str]:
    """Get tags for an intent. Prefers resolved canonical; falls back
    to the literal text underscored.

    Decompose intent.text frequently includes leading verbs (`wearing
    red socks`, `with red gloves`, `in a fancy bed`) that don't
    canonicalize on their own — the canonical form is just the
    object phrase. Strip common leading verbs before the fallback so
    `wearing red socks` falls back to `red_socks` (a real Danbooru
    tag), not `wearing_red_socks` (which trace-check would drop).
    """
    resolved = intent.get("_resolved_tags") or []
    if resolved:
        return list(resolved)
    if intent.get("_suppress_literal_fallback"):
        return []
    text = (intent.get("text") or "").strip()
    if not text:
        return []
    lc = text.lower()
    for prefix in _LEADING_VERB_PREFIXES:
        if lc.startswith(prefix):
            text = text[len(prefix):].strip()
            lc = text.lower()
            break
    if not text:
        return []
    return [text.lower().replace(" ", "_").replace("-", "_")]


def _apply_outfit_borrow_to_sections(
    sections: list[dict], source_bio: dict, primary_bio: dict,
) -> list[dict]:
    """Replace primary's outfit with source_bio's outfit, with the
    `// Outfit: <source name> from Character: <source tag>` header."""
    outfit = (source_bio.get("default_outfit") or {})
    slots = outfit.get("slots") or []
    if not slots and not (outfit.get("tags") or "").strip():
        return sections
    new_tokens = _api._bio_outfit_tokens(source_bio)
    if not new_tokens:
        return sections
    source_name = (outfit.get("name") or "").strip()
    source_tag = (source_bio.get("tag") or "").strip()
    new_header = f"// Outfit: {source_name} from Character: {source_tag}".strip()

    out: list[dict] = []
    overwrote = False
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        if (s.get("header") or "").lower().startswith("// outfit"):
            out.append({
                "header": new_header, "tokens": list(new_tokens),
                "body_text": "", "is_negative": False,
            })
            overwrote = True
            continue
        out.append(s)
    if not overwrote:
        # No outfit section to replace — insert one after the first character
        for i, s in enumerate(out):
            if (s.get("header") or "").lower().startswith("// character"):
                out.insert(i + 1, {
                    "header": new_header, "tokens": list(new_tokens),
                    "body_text": "", "is_negative": False,
                })
                break
    return out


# ── Stage 4: coherence (deterministic) ──────────────────────────────


def _apply_coherence(
    sections: list[dict], bios: list[dict],
    user_request: str, node_prompt: str,
    model_hash: str, request_id: str,
    intent_added_tokens: set[str] | None = None,
    outfit_was_rewritten: bool = False,
) -> list[dict]:
    """Run the 8 SDXL strategies + structural composition. Reuses
    the existing post-pass functions verbatim — they already operate
    on assembled section lists, so they work whether the assembly
    came from the LLM or from rails compose."""
    # Modifier detection from user_request (alias scan)
    try:
        detected = _api._detect_modifiers_in_text(user_request) or []
    except Exception:
        detected = []
    applies_by_tag = {d["canonical_tag"]: d for d in detected}

    # 0. Color-swap pre-pass — `red leotard` against bio's `green_leotard`
    #    mutates the slot in-place + records displaced phrase. Runs
    #    BEFORE compose so the new color lands in the // Outfit section
    #    directly when compose builds it. In patch mode, also re-runs
    #    after sections are built (below) to catch new tokens user added.
    try:
        _api._resolve_color_swaps(bios, user_request)
    except Exception:
        logger.exception("tag-rails: _resolve_color_swaps failed")

    # 1. Modifier clear pre-pass — drop bio slots that modifiers clear
    #    (e.g. barefoot clears footwear/legwear). Mutates bios in
    #    place; records displaced phrases for later negation.
    modifier_clear_fired = False
    if detected:
        try:
            before_disp = {id(b): list(b.get("_displaced_phrases") or [])
                           for b in bios or []}
            _api._resolve_modifier_clears(bios, user_request)
            for b in bios or []:
                after_disp = list(b.get("_displaced_phrases") or [])
                if after_disp != before_disp.get(id(b), []):
                    modifier_clear_fired = True
                    break
        except Exception:
            logger.exception("tag-rails: _resolve_modifier_clears failed")

    # 2. Re-build outfit sections from bios IF modifier-clear mutated
    #    them. In pure patch mode without modifier triggers, the
    #    node_prompt's existing outfit tokens are authoritative — we
    #    don't want to re-pull from bio (which would pad the section
    #    with slots the user removed from their prompt earlier).
    # Only refresh outfit from bio when modifier-clear fired AND the
    # LLM rewrite didn't already produce a fresh outfit. If the user
    # named an outfit change (e.g. 'wearing pink micro bikini'), the
    # LLM's output is authoritative; refresh would clobber it.
    if modifier_clear_fired and not outfit_was_rewritten:
        sections = _refresh_outfit_sections_from_bios(sections, bios)

    # 3. Enforce [APPLIES] modifiers — inject canonical to the right
    #    section (Pose for substitutes, Outfit for implies cascade)
    sections = _api._enforce_applies_modifiers(
        sections, applies_by_tag, request_id,
    )

    # 4. Modifier-clear post-pass — drop output tokens that match
    #    cleared slot names (defense in depth if compose missed any)
    sections = _api._apply_modifier_clear_post_pass(
        sections, user_request, bios, request_id,
    )

    # 5. Multi-character: aggregate count + BREAK separators
    sections = _api._enforce_multi_char_composition(
        sections, bios, request_id,
    )

    # 5b. Posture-conflict resolution. When user names a posture
    #    (standing, kneeling, lying, crouching), drop conflicting
    #    postures from the prior turn. The LLM rewrite alone can't
    #    do this consistently — the action hint ('add') says "keep
    #    everything" but posture replacement is implicit.
    try:
        sections = _api._resolve_posture_conflicts(
            sections, user_request, node_prompt, request_id,
        )
    except Exception:
        logger.exception("tag-rails: _resolve_posture_conflicts failed")

    # 6. Default-outfit auto-negation (push displaced phrases into
    #    Negative). Uses bio._displaced_phrases populated by the
    #    modifier-clear pre-pass.
    sections = _api._enforce_default_outfit_negation(sections, bios)


    # 7. Trace-check — every emitted positive token must trace to bio /
    #    modifier canonical / node_prompt / tag-wiki retrieval / user
    #    text / danbooru_tags. Without this, canonical_resolver noise
    #    (bge cosine on vague intent text returning random scene tags
    #    like 'ancient ruin, cityscape, desert' for 'expand on the bed')
    #    survives into the output. Reuse the legacy filter verbatim.
    try:
        from . import natlang_facts as _nlf  # noqa: F401
    except Exception:
        pass
    try:
        all_modifiers = _api._load_slot_modifiers()
    except Exception:
        all_modifiers = []
    try:
        sections = _api._drop_untraceable_tokens(
            sections, user_request, bios, all_modifiers, request_id,
            node_prompt=node_prompt,
        )
    except Exception:
        logger.exception("tag-rails: _drop_untraceable_tokens failed")


    # Also drop section-header-like strings that leaked in as tokens
    # (e.g. `// setting / scene` showing up inside a // Setting body).
    sections = _strip_header_like_tokens(sections, request_id)

    # 8. Section-mismatch filter — tokens in the wrong section get
    #    dropped (defense in depth; tag-domain section index lookup).
    try:
        sections = _api._drop_misplaced_tokens(sections, request_id)
    except Exception:
        logger.exception("tag-rails: _drop_misplaced_tokens failed")

    # 8b. Negative-section carry-forward — when patching an existing
    #    prompt that has user-curated negatives, they survive unless
    #    a current intent contradicts them.
    if (node_prompt or "").strip():
        try:
            sections = _api._preserve_existing_negatives(sections, node_prompt)
        except Exception:
            logger.exception("tag-rails: _preserve_existing_negatives failed")

    # 9. Character-swap negative cleanup. MUST run after carry-forward
    #    or carry-forward re-introduces the prior character's defaults
    #    pulled from node_prompt. Drops preserved negs that were
    #    auto-pushed by a prior turn's default-outfit negation against
    #    the OLD character.
    if (node_prompt or "").strip():
        try:
            sections = _api._scrub_prior_character_default_negs(
                sections, bios, node_prompt, request_id,
            )
        except Exception:
            logger.exception("tag-rails: _scrub_prior_character_default_negs failed")

    # 10a. Strip intent-added tokens from Negative BEFORE the legacy
    # dedupe runs. Legacy treats Negative as authoritative ('neg wins'
    # in case of conflict), but when the user just added a positive
    # via intent this turn (e.g. 'wearing red socks' after a prior
    # 'barefoot' turn pushed red_socks to Negative), the new positive
    # must win. Strips intent-added canonicals from negatives so the
    # subsequent dedupe sees no conflict.
    if intent_added_tokens:
        sections = _strip_intent_tokens_from_negatives(
            sections, intent_added_tokens, request_id,
        )

    # 10b. Dedupe — for any remaining positive/negative conflicts that
    # WEREN'T intent-driven (e.g. existing legacy state preserved from
    # node_prompt), Negative still wins per legacy semantics.
    try:
        sections = _api._dedupe_negatives_from_positives(sections)
    except Exception:
        logger.exception("tag-rails: _dedupe_negatives_from_positives failed")

    return sections


def _strip_header_like_tokens(
    sections: list[dict], request_id: str,
) -> list[dict]:
    """Drop tokens that look like section headers (start with `//`).
    These leak in when canonical_resolver or LLM hallucinates a token
    that's actually a section reference. Saw `// setting / scene`
    appearing inside `// Setting / Scene` body in dev test 22:38."""
    dropped: list[str] = []
    for s in sections:
        if s.get("is_negative"):
            continue
        kept = []
        for t in s.get("tokens") or []:
            ts = (t or "").strip()
            if ts.startswith("//"):
                dropped.append(t)
                continue
            kept.append(t)
        s["tokens"] = kept
    if dropped:
        logger.info(
            "tag-rails[%s] stripped header-like tokens: %s",
            request_id, ", ".join(dropped),
        )
    return sections


def _refresh_outfit_sections_from_bios(
    sections: list[dict], bios: list[dict],
) -> list[dict]:
    """After modifier-clear pre-pass mutates bio outfit slots, the
    output's // Outfit sections may have stale tokens. Rebuild each
    // Outfit section from its matching bio's current slot data."""
    # Build a map: (lowercased outfit name) -> bio
    bio_by_outfit_name: dict[str, dict] = {}
    for b in bios or []:
        if not b or b.get("_outfit_source_only"):
            continue
        outfit = b.get("user_requested_outfit") or b.get("default_outfit") or {}
        name = (outfit.get("name") or "").strip().lower()
        if name:
            bio_by_outfit_name[name] = b

    out: list[dict] = []
    for s in sections:
        if s.get("is_negative"):
            out.append(s)
            continue
        header_lc = (s.get("header") or "").lower()
        if not header_lc.startswith("// outfit"):
            out.append(s)
            continue
        # Extract the outfit name from the header
        after_colon = header_lc.split(":", 1)[1].strip() if ":" in header_lc else ""
        # Strip "from character: X" suffix if present
        if " from character:" in after_colon:
            after_colon = after_colon.split(" from character:", 1)[0].strip()
        bio = bio_by_outfit_name.get(after_colon)
        if not bio:
            out.append(s)
            continue
        new_tokens = _api._bio_outfit_tokens(bio)
        if new_tokens:
            new_s = dict(s)
            new_s["tokens"] = new_tokens
            out.append(new_s)
        else:
            out.append(s)
    return out


# ── Output assembly ─────────────────────────────────────────────────


def _assemble_output_text(sections: list[dict], tag_format: str) -> str:
    """Assemble final output_text from sections. Mirrors the
    `_format_output` style of the legacy pipeline."""
    out_lines: list[str] = []
    for s in sections:
        header = (s.get("header") or "").strip()
        tokens = s.get("tokens") or []
        body = ", ".join(tokens)
        if header and body:
            out_lines.append(f"{header}\n{body}")
        elif header:
            out_lines.append(header)
    text = "\n\n".join(out_lines)
    if tag_format == "spaces":
        # Underscore -> space conversion happens via ai_api helper —
        # reuse so the same escaping rules apply.
        try:
            text = _api._underscore_to_space(text)
        except AttributeError:
            text = text.replace("_", " ")
    return text
