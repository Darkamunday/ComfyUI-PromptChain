"""Hybrid edit-path router.

KB injects, fragment-rewrite modifies. Whole-section ops + KB-resolvable
swaps go through rails-v2 (build-mode, character/outfit/pose/style
inserts, locate-infill swaps). Modifications and sub-slot edits go
through the fragment-rewrite path (atomize -> classify -> per-atom
isolated rewrite). After a rails sub-slot replace, a coherence pass
catches stale cross-section references.

Single entry point: `run_hybrid_turn(prompt, user_request, model_hash=)`.
Returns a trace dict shaped compatibly with rails-v2's run_turn_v2 so
ai_api.py can swap one for the other behind a feature flag.
"""
from __future__ import annotations

import logging
import re

from scripts.natlang_rails_v2 import run_turn_v2
from scripts.natlang_decompose_probe import decompose
from scripts.natlang_fragment_rewrite_harness import (
    run_modification, atomize, splice,
)
from core import ai_api


logger = logging.getLogger("promptchain.ai.hybrid")


def _preview(s: str | None, n: int = 100) -> str:
    s = (s or "").replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"...({len(s)}c)"


# ── Routing ───────────────────────────────────────────────────────


FRAGMENT_OPS = {"anatomy_mod", "modify", "remove"}
SUB_SLOTS = {
    # outfit sub-slots
    "tops", "bottoms", "footwear", "legwear", "handwear",
    "armwear", "headwear", "neckwear", "accessories",
    # character sub-slots
    "hair", "eyes", "body", "face",
}
BODY_STATE_WORDS = {
    "barefoot", "barefooted", "bareheaded", "bare-handed", "topless",
    "bottomless", "shirtless", "naked", "nude", "going commando",
}


def route_intent(intent: dict) -> str:
    """Return 'rails' or 'fragment' for a single decompose intent.

    Rails handles ops that benefit from KB resolution, section-header
    insertion, or deterministic slot-content swaps (`replace`/`add`).
    Fragment handles modifications that qualify existing prose
    (`anatomy_mod`/`modify`/`remove`).

    Sub-slot swaps (footwear barefoot → red socks) intentionally go to
    rails: locate-infill produces a clean SEARCH/REPLACE that the
    coherence pass then propagates to cross-section references. The
    fragment path is unreliable here (model often appends rather than
    replaces, or rewrites multiple atoms redundantly).
    """
    op = (intent.get("op") or "replace").lower()
    if op in FRAGMENT_OPS:
        return "fragment"
    return "rails"


def is_sub_slot_swap(intent: dict) -> bool:
    op = (intent.get("op") or "replace").lower()
    concept = (intent.get("concept") or "").lower()
    return op in ("replace", "add") and concept in SUB_SLOTS


SENTENCE_CONCEPTS = {
    "character", "subject", "outfit", "pose", "expression",
    "scene", "style", "quality",
}


def synthesize_fragment_request(intent: dict) -> str:
    """Build a natural-language single-intent request for the fragment
    rewriter, scoped to the concept's section so the classifier doesn't
    over-include atoms in unrelated sections."""
    op = (intent.get("op") or "replace").lower()
    concept = (intent.get("concept") or "").lower()
    text = (intent.get("text") or "").strip()
    if not text:
        return ""

    if op == "modify" and concept in SENTENCE_CONCEPTS:
        return f"modify ONLY the {concept} section to reflect: {text}. Leave every other section unchanged. Never touch the Negative Prompt."
    if op == "modify" and concept in {
        "tops", "bottoms", "footwear", "legwear", "handwear",
        "armwear", "headwear", "neckwear", "accessories",
    }:
        return (
            f"modify ONLY the {concept} item inside the Outfit section "
            f"to be {text}. Leave the Character, Pose, Style, and "
            f"Negative Prompt sections completely unchanged. If no "
            f"{concept} item is present in the Outfit, change nothing."
        )
    if op == "modify" and concept in {"hair", "eyes", "body", "face"}:
        return (
            f"modify ONLY the {concept} description inside the Character "
            f"section to be {text}. Leave every other section "
            f"unchanged. Never touch the Negative Prompt."
        )
    if op == "anatomy_mod":
        # Anatomy mods may legitimately ripple across sections
        # (e.g. character body + outfit footwear slot + pose reference).
        return f"give her {text}. Never touch the Negative Prompt."
    if op == "modify":
        return f"modify the {concept}: {text}. Never touch the Negative Prompt."
    if op == "remove":
        return f"remove {text}"
    if op in ("replace", "add"):
        if text.lower() in BODY_STATE_WORDS:
            return f"make her {text}"
        if concept in {
            "footwear", "legwear", "handwear", "headwear", "neckwear",
            "armwear", "tops", "bottoms", "accessories",
        }:
            return f"wearing {text}"
        if concept in {"hair", "eyes", "body", "face"}:
            return f"her {concept}: {text}"
        return text
    return text


# ── Coherence pass (post-rails sub-slot replace) ─────────────────


_STOPWORDS = {"with", "and", "the", "for", "from", "into", "onto",
              "of", "in", "on", "at", "to", "is", "an", "a"}


def _content_words(phrase: str) -> set[str]:
    return {
        w.lower() for w in re.findall(r"[A-Za-z']+", phrase or "")
        if len(w) >= 4 and w.lower() not in _STOPWORDS
    }


HARMONIZE_SYSTEM = """You are fixing stale references in a natural-language image-generation prompt.

A region of the prompt was just changed. The OLD phrase has been replaced by the NEW phrase in the parent sentence. Some OTHER sentences may still reference the old state — either by quoting OLD verbatim, by quoting part of OLD, or by describing the body region in a way that now contradicts NEW.

For EACH numbered fragment below, decide:
  - If it contains a stale reference to OLD (or to the body region OLD described, in a way contradicted by NEW), rewrite the fragment so its references align with NEW.
  - Otherwise, output the literal token UNCHANGED.

REWRITE RULES:
  - Replace stale wording, don't append new clauses.
  - Keep grammar tight. The rewritten sentence should read naturally.
  - Do not invent details not in NEW.

OUTPUT FORMAT — one line per fragment:
  1. <rewritten or UNCHANGED>
  2. <rewritten or UNCHANGED>
  ...
No commentary outside the numbered lines."""


_PROVIDER = "local"
_CONFIG = {"local": {"base_url": "http://localhost:11434/v1",
                     "model": "qwen3-vl:8b-instruct"}}


async def harmonize_refs(prompt: str, old: str, new: str,
                         parent_sentence: str) -> str:
    """Find references to OLD outside the parent sentence and update
    them to align with NEW. Content-word pre-filter prevents the LLM
    from touching atoms unrelated to the changed region."""
    atoms = atomize(prompt)
    old_words = _content_words(old)
    if not old_words:
        return prompt
    candidates: list[int] = []
    for i, atom in enumerate(atoms):
        if atom.section.lower() == "negative":
            continue
        if atom.text in parent_sentence or parent_sentence in atom.text:
            continue
        if _content_words(atom.text) & old_words:
            candidates.append(i)
    if not candidates:
        return prompt
    numbered = "\n".join(
        f"{k + 1}. {atoms[i].text}" for k, i in enumerate(candidates)
    )
    user_msg = (
        f"OLD phrase: {old}\n"
        f"NEW phrase: {new}\n\n"
        f"FRAGMENTS to check for stale references:\n{numbered}\n\n"
        f"Output numbered results:"
    )
    raw = await ai_api._run_generation(
        f"hybrid-harmonize-{abs(hash((old, new, prompt))) % 10000}",
        _PROVIDER, _CONFIG,
        HARMONIZE_SYSTEM, user_msg, [],
    )
    rewrites: dict[int, str] = {}
    line_re = re.compile(r"^\s*(\d+)\.\s*(.*?)\s*$")
    for line in (raw or "").splitlines():
        m = line_re.match(line)
        if not m:
            continue
        local_idx = int(m.group(1)) - 1
        text = m.group(2).strip()
        if not (0 <= local_idx < len(candidates)):
            continue
        atom_idx = candidates[local_idx]
        if text.upper().strip().rstrip(".") == "UNCHANGED":
            continue
        if text == atoms[atom_idx].text:
            continue
        if (text.startswith('"') and text.endswith('"')) \
                or (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()
        if text:
            rewrites[atom_idx] = text
    return splice(prompt, atoms, rewrites)


# ── Orchestrator ─────────────────────────────────────────────────


async def run_hybrid_turn(prompt: str, user_request: str,
                          model_hash: str | None = None,
                          bios: list[dict] | None = None) -> dict:
    """One hybrid turn. Build mode → rails. Edit mode → decompose,
    split intents by route, run rails on rails-routed, fragment on
    fragment-routed, then coherence pass for sub-slot swaps.

    `bios` is the chat agent's preflight character match list,
    forwarded to run_turn_v2 so pronoun-only build requests still
    emit a // Character section."""
    is_build = not (prompt or "").strip()
    logger.info(
        "hybrid: turn start | build_mode=%s prompt_chars=%d request=%r",
        is_build, len(prompt or ""), (user_request or "")[:200],
    )
    if is_build:
        # Decompose first so we can dedupe multi-intent same-concept
        # turns. Rails' sentence dispatcher treats op=modify on an
        # already-populated section as a wholesale REPLACE — which
        # nukes the rich KB body inserted by the prior intent
        # (e.g. 'bedroom scene' lands Bedroom KB body, then 'golden
        # lighting' as scene-modify wipes it). Split per-concept:
        # primary intents (first per sentence-concept) go to rails;
        # secondary intents (subsequent same-concept) run through
        # fragment-rewrite AFTER rails builds the base prompt.
        from scripts.natlang_dispatch import (
            SENTENCE_CONCEPTS, normalize_concept,
        )
        decomposed_b, _ = await decompose(user_request)
        intents_b = list(decomposed_b.get("intents") or [])
        primary: list[dict] = []
        secondary: list[dict] = []
        seen_sentence: set[str] = set()
        for it in intents_b:
            concept_n = normalize_concept(it.get("concept") or "")
            if concept_n in SENTENCE_CONCEPTS and concept_n in seen_sentence:
                # Force op=modify so fragment treats it as a qualifier
                # rather than a wholesale swap.
                it_copy = dict(it)
                it_copy["op"] = "modify"
                secondary.append(it_copy)
            else:
                primary.append(it)
                if concept_n in SENTENCE_CONCEPTS:
                    seen_sentence.add(concept_n)
        logger.info(
            "hybrid: build-mode split: primary=%d secondary=%d",
            len(primary), len(secondary),
        )
        primary_decomposed = {**decomposed_b, "intents": primary}
        trace = await run_turn_v2(
            "", user_request,
            model_hash=model_hash,
            pre_decomposed=primary_decomposed,
            bios=bios,
        )
        trace["pipeline"] = "hybrid"
        routing = [("rails-build", user_request)]
        current = trace.get("final_prompt") or ""
        # Apply secondary intents via fragment on the rails-built prompt.
        for intent in secondary:
            synth = synthesize_fragment_request(intent)
            routing.append(("fragment-secondary", synth))
            if not synth:
                continue
            logger.info(
                "hybrid: build-mode secondary fragment intent=%s synth=%r",
                intent.get("concept"), _preview(synth, 200),
            )
            result = await run_modification(current, synth)
            current = result["final_prompt"]
        trace["final_prompt"] = current
        trace["routing"] = routing
        return trace

    decomposed, _ = await decompose(user_request)
    intents = decomposed.get("intents") or []

    # Edit-mode downgrade: when a sentence-shaped section already
    # exists in the prompt and decompose emitted op=replace, treat
    # the user's request as a partial modification rather than a
    # wholesale swap. "her arms up behind her head" on an existing
    # Victory Pose should weave into the body, not nuke it.
    # Outfit/character/style stay on op=replace because those are
    # genuine swaps that benefit from rails' KB resolution.
    _DOWNGRADE_REPLACE_TO_MODIFY = {"pose", "expression", "scene"}
    for intent in intents:
        concept = (intent.get("concept") or "").lower()
        op = (intent.get("op") or "").lower()
        if op == "replace" and concept in _DOWNGRADE_REPLACE_TO_MODIFY:
            if re.search(rf"(?im)^//\s*{re.escape(concept)}\b", prompt):
                logger.info(
                    "hybrid: downgrading %s op=replace -> op=modify "
                    "(section already populated, treat as partial edit)",
                    concept,
                )
                intent["op"] = "modify"

    rails_intents: list[dict] = []
    fragment_intents: list[dict] = []
    for intent in intents:
        if route_intent(intent) == "rails":
            rails_intents.append(intent)
        else:
            fragment_intents.append(intent)

    logger.info(
        "hybrid: decompose intents=%d rails=%d fragment=%d",
        len(intents), len(rails_intents), len(fragment_intents),
    )
    for i, it in enumerate(intents):
        logger.info(
            "hybrid:   intent[%d] concept=%s op=%s text=%r route=%s",
            i, it.get("concept"), it.get("op"),
            _preview(it.get("text"), 120),
            route_intent(it),
        )

    current = prompt
    routing_log: list[tuple[str, str]] = []
    summary_intents: list[dict] = []
    rails_trace: dict | None = None

    if rails_intents:
        # Pass pre-decomposed intents through to rails-v2 instead of
        # comma-joining their texts into a string. The string-join path
        # squashed multi-char subjects: hybrid's careful split (intent[0]
        # = subject Tifa replace, intent[1] = subject Chun-Li add) became
        # 'Tifa Lockhart, Chun-Li' as a single rails subject intent,
        # which then collapsed both characters into bare names and
        # destroyed the Tifa bio prose. Build mode already used this
        # pre_decomposed handoff correctly; edit mode now matches it.
        rails_pre_decomposed = {**decomposed, "intents": rails_intents}
        rails_text = ", ".join(
            (i.get("text") or "").strip() for i in rails_intents
            if (i.get("text") or "").strip()
        ) or user_request
        routing_log.append(("rails", rails_text))
        rails_trace = await run_turn_v2(
            current, rails_text,
            model_hash=model_hash,
            pre_decomposed=rails_pre_decomposed,
            bios=bios,
        )
        current = rails_trace.get("final_prompt") or current
        for it in rails_trace.get("intents") or []:
            summary_intents.append({**it, "route": "rails"})

    for intent in fragment_intents:
        synth = synthesize_fragment_request(intent)
        routing_log.append(("fragment", synth))
        if not synth:
            logger.warning("hybrid:   fragment intent had empty synth, skipping")
            continue
        logger.info("hybrid:   fragment synth=%r", _preview(synth, 200))
        result = await run_modification(current, synth)
        # Per-atom before/after dump so the log explains exactly what
        # the fragment path edited (or refused to edit).
        atoms = result.get("atoms") or []
        affected = result.get("affected_indices") or []
        rewrites = result.get("rewrites") or {}
        logger.info(
            "hybrid:   fragment classify affected=%s (out of %d atoms)",
            [i + 1 for i in affected], len(atoms),
        )
        # Capture before/after pairs from sub-slot sections so the
        # coherence pass below can propagate references to OLD content
        # that may still live in OTHER sentences (pose mentions the
        # outfit's footwear color, etc).
        coherence_pairs: list[tuple[str, str, str]] = []
        for idx in affected:
            before = atoms[idx].text if idx < len(atoms) else ""
            section = atoms[idx].section if idx < len(atoms) else ""
            if idx in rewrites:
                logger.info(
                    "hybrid:     atom[%d/%s] REWROTE %r -> %r",
                    idx + 1, section,
                    _preview(before, 100),
                    _preview(rewrites[idx], 100),
                )
                # Outfit and character sections hold sub-slot items
                # whose color/state can be quoted by pose/scene/style.
                if section.lower() in {"outfit", "character"}:
                    coherence_pairs.append((before, rewrites[idx], section))
            else:
                logger.info(
                    "hybrid:     atom[%d/%s] kept   %r (rewrite vetoed or unchanged)",
                    idx + 1, section,
                    _preview(before, 100),
                )
        current = result["final_prompt"]
        # Fragment-side coherence: for each rewritten sub-slot atom,
        # check whether OTHER sentences still quote the OLD text and
        # update them to align with the NEW. Same pass shape as the
        # rails-side coherence below (deterministic substring + LLM
        # harmonize_refs with content-word pre-filter).
        for before_text, after_text, section in coherence_pairs:
            if not before_text or not after_text:
                continue
            if before_text == after_text:
                continue
            # Skip coherence when NEW contains OLD as a substring.
            # That signals a qualifier extension (e.g. 'barefoot' ->
            # 'barefoot with bigger feet') — the OLD content is STILL
            # semantically true everywhere it was quoted, so other
            # sentences don't need updating. Running coherence here
            # double-applies the qualifier inside the rewritten atom
            # itself ('barefoot with bigger feet with bigger feet').
            if before_text in after_text:
                logger.info(
                    "hybrid:   fragment-coherence skipped (qualifier extension; "
                    "OLD %r is substring of NEW %r)",
                    _preview(before_text, 60), _preview(after_text, 60),
                )
                continue
            # Deterministic pass first: look for OTHER occurrences of
            # the OLD text in the prompt. The parent section atom we
            # already rewrote has been replaced — any remaining
            # occurrence is a cross-section quote.
            if before_text in current:
                logger.info(
                    "hybrid:   fragment-coherence-sub %r -> %r",
                    _preview(before_text, 80), _preview(after_text, 80),
                )
                current = current.replace(before_text, after_text)
            # LLM harmonize for partial references (pose says
            # "presenting red socks at viewer" — substring of
            # "red socks" exists but only as part of the pose phrase).
            logger.info(
                "hybrid:   fragment-coherence-llm old=%r new=%r",
                _preview(before_text, 80), _preview(after_text, 80),
            )
            # Find the parent sentence in the post-rewrite prompt so
            # harmonize_refs skips it (the parent already has the new
            # content; we only want to fix OTHER sentences).
            parent_sentence = ""
            for line in current.splitlines():
                if after_text in line:
                    parent_sentence = line
                    break
            current = await harmonize_refs(
                current, before_text, after_text, parent_sentence,
            )
        summary_intents.append({
            "concept": intent.get("concept"),
            "op": intent.get("op"),
            "text": intent.get("text"),
            "route": "fragment",
            "synth": synth,
            "rewrites": {k: v for k, v in rewrites.items()},
        })

    # Coherence pass on rails-completed sub-slot replaces.
    if rails_trace:
        for intent_trace in (rails_trace.get("intents") or []):
            concept = (intent_trace.get("concept") or "").lower()
            if concept not in SUB_SLOTS:
                continue
            search = (intent_trace.get("search") or "").strip()
            replace = (intent_trace.get("replace") or "").strip()
            if not search or search == "(not present)" or not replace:
                continue
            if search in current:
                routing_log.append(
                    ("coherence-sub", f"{search!r} -> {replace!r}"),
                )
                current = current.replace(search, replace)
            after_text = (intent_trace.get("after") or current)
            parent_sentence = ""
            for line in after_text.splitlines():
                if replace in line:
                    parent_sentence = line
                    break
            routing_log.append(
                ("coherence-llm", f"old={search!r} new={replace!r}"),
            )
            current = await harmonize_refs(
                current, search, replace, parent_sentence,
            )

    logger.info(
        "hybrid: turn done | routes=%s final_chars=%d preview=%r",
        [r[0] for r in routing_log], len(current),
        _preview(current, 400),
    )

    return {
        "pipeline": "hybrid",
        "final_prompt": current,
        "decompose": decomposed,
        "intents": summary_intents,
        "routing": routing_log,
    }
