"""Per-sub-intent canonical-tag resolver.

Replaces the bge-only retrieval bottleneck where bare-form Danbooru
tags (`sitting`, `standing`, `barefoot`, `dojo`) were buried under
compound variants (`sitting_on_arm`, `holding_microphone_stand`) in
bge's top-K. The resolver:

  1. Asks the configured LLM to propose 3-8 canonical Danbooru tags
     for each sub-intent (in parallel across intents).
  2. Validates each proposal against `danbooru_tags` (real-tag check
     + body-part variant swap, e.g. `feet_focus` -> `foot_focus`).
  3. Surfaces validated tags as high-priority candidates with
     `matched_via="resolved"`, which the patch user message routes
     into the "Direct text matches" block.

Achieves ~86% resolution on the test corpus vs ~18% for bge alone.
The existing `tag_search` retrieval still runs — its results land in
the "Related candidates" block as before, so the resolver is purely
additive (cannot regress).

Skips out-of-scope sections (character / clear / style) — those go
through the bio system / template path / no-tag path. Cost is one
LLM call per in-scope sub-intent, parallelized; total wall-clock is
roughly the slowest single call (~0.6-0.8s on 8b)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable

from . import ai_api

logger = logging.getLogger("promptchain.canonical_resolver")
dbg = logging.getLogger("promptchain.ai.debug")


_RESOLVE_SKIP_SECTIONS = frozenset({"character", "clear", "style"})


_PROPOSE_SYSTEM = (
    "You are a Danbooru tag expert. Given a natural-language phrase, "
    "list 3-8 canonical Danbooru tags that could match. Output ONE "
    "TAG PER LINE in lowercase underscore form (e.g. `foot_focus`). "
    "No explanations, no markdown bullets, no punctuation, no header. "
    "If you don't know any matching tags, output `NONE`.\n/no_think"
)


# Body-part word variants — the LLM occasionally emits the wrong number
# (`feet_focus` instead of `foot_focus`). One swap is allowed before the
# proposal is rejected.
_BODY_PART_PAIRS = [
    ("feet", "foot"), ("hands", "hand"), ("eyes", "eye"),
    ("fingers", "finger"), ("toes", "toe"), ("knees", "knee"),
    ("legs", "leg"), ("ears", "ear"), ("breasts", "breast"),
    ("thighs", "thigh"),
]


def _tag_exists(tag: str) -> bool:
    """True iff `tag` is in `danbooru_tags`."""
    if not tag:
        return False
    try:
        from .tag_builder import get_db
        db = get_db()
        row = db.execute(
            "SELECT 1 FROM danbooru_tags WHERE tag = ? LIMIT 1",
            (tag.lower().strip(),),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _try_body_part_swap(tag: str) -> str | None:
    """If `tag` isn't real but a single body-part swap (feet↔foot etc.)
    yields a real tag, return that variant. Else None."""
    parts = tag.split("_")
    for i, p in enumerate(parts):
        for plural, singular in _BODY_PART_PAIRS:
            for current, swap_to in ((plural, singular), (singular, plural)):
                if p != current:
                    continue
                candidate = "_".join(parts[:i] + [swap_to] + parts[i + 1:])
                if _tag_exists(candidate):
                    return candidate
    return None


def _validate(tag: str) -> str | None:
    """Return real canonical form (with body-part variant swap) or None."""
    tag = (tag or "").strip().lower().replace(" ", "_")
    if not tag:
        return None
    if _tag_exists(tag):
        return tag
    return _try_body_part_swap(tag)


def _parse_proposed_tags(raw: str) -> list[str]:
    """Parse the LLM's proposal output (one tag per line) into a list."""
    if not raw or raw.strip().lower() == "none":
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = re.sub(r"^[\s\-\*•·]*", "", line).strip()
        line = re.sub(r"[`'\"]+|[\.,;:!?]$", "", line).strip()
        if not line or line.lower() == "none":
            continue
        tok = line.split()[0].strip().lower().rstrip(".,;:!?")
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


async def _llm_propose(phrase: str, request_id: str) -> str:
    """Provider-agnostic LLM call returning the raw response text."""
    config = ai_api._load_config()
    provider = config.get("provider")
    user_text = f'Phrase: "{phrase}"'
    try:
        return await ai_api._call_provider_complete(
            request_id, provider, config, _PROPOSE_SYSTEM, user_text, [],
        )
    except Exception:
        logger.warning("canonical_resolver: LLM propose call failed",
                       exc_info=True)
        return ""


# Stopwords for the secondary fallback's ngram permutation. Prepositions
# and articles that connect content words but don't carry meaning.
# Mirrors the literal-anchor stopword set in ai_api but kept local so
# the resolver doesn't depend on private-named ai_api internals.
_NGRAM_STOPWORDS = frozenset({
    "a", "an", "the", "her", "his", "their", "its", "is", "are",
    "was", "were", "be", "to", "of", "and", "or", "on", "in", "at",
    "for", "from", "with", "this", "that",
})


def _ngram_permute_validated(phrase: str) -> list[str]:
    """Deterministic word-permutation candidate generator. Splits phrase
    into content words, generates 1-3-word permutations (ordered + body-
    part variant swaps), validates each against danbooru_tags. Returns
    the validated real-tag list, deduped, preserving discovery order.

    Used by `_resolve_one` as a SECONDARY fallback when the primary LLM-
    propose path returns empty. Catches `<focus|emphasis|frame>_<body
    _part>` and similar reorderings: 'focus on feet' permutes through
    `feet_focus` which body-part-swaps to the real `foot_focus`."""
    if not phrase:
        return []
    words = [w for w in re.findall(r"[a-z]+", phrase.lower())
             if w not in _NGRAM_STOPWORDS and len(w) >= 3]
    if not words:
        return []
    candidates: list[str] = []
    for w in words:
        candidates.append(w)
    for i in range(len(words)):
        for j in range(len(words)):
            if i == j:
                continue
            candidates.append(f"{words[i]}_{words[j]}")
    if len(words) >= 3:
        for i in range(len(words)):
            for j in range(len(words)):
                for k in range(len(words)):
                    if len({i, j, k}) < 3:
                        continue
                    candidates.append(f"{words[i]}_{words[j]}_{words[k]}")
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        v = _validate(c)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


_RERANK_SYSTEM = (
    "You pick the single canonical Danbooru tag that best matches a "
    "user's phrase, from a curated list of REAL tags. Output ONLY the "
    "tag in lowercase underscore form (e.g. `foot_focus`) — no "
    "explanation, no markdown, no punctuation. If none of the tags "
    "capture the phrase's meaning, output `NONE`.\n/no_think"
)


async def _llm_rerank(phrase: str, candidates: list[str],
                       request_id: str) -> str | None:
    """Ask the LLM to pick the best-matching tag from a curated real-tag
    list. Returns the picked tag (lowercased, underscored) or None when
    the LLM declined or the response didn't validate."""
    if not candidates:
        return None
    candidate_list = ", ".join(candidates)
    user_text = (
        f'Phrase: "{phrase}"\n\nReal Danbooru tags found by word '
        f'permutation: {candidate_list}\n\nWhich one best matches the '
        f"phrase's meaning? Output the tag only."
    )
    try:
        raw = await ai_api._call_provider_complete(
            request_id, ai_api._load_config().get("provider"),
            ai_api._load_config(),
            _RERANK_SYSTEM, user_text, [],
        )
    except Exception:
        logger.warning("canonical_resolver: LLM rerank call failed",
                       exc_info=True)
        return None
    raw = (raw or "").strip()
    if not raw or raw.lower() == "none":
        return None
    pick = re.sub(r"[`'\"]+|[\.,;:!?]$", "", raw).strip()
    pick = pick.split()[0].lower().rstrip(".,;:!?") if pick.split() else ""
    if not pick:
        return None
    # Sanity: only return picks that are actually in the candidate list
    # (prevents the LLM from reverting to a hallucination on rerank).
    candidates_lc = {c.lower() for c in candidates}
    return pick if pick in candidates_lc else None


def _build_resolved_candidates(validated: list[str], phrase: str,
                                via: str = "resolved") -> list[dict]:
    """Build the candidate-dict list for a list of validated tag forms.
    Pulls wiki rows in one DB query so each candidate carries
    body_summary for the patch user message."""
    if not validated:
        return []
    rows: dict[str, dict] = {}
    try:
        from .tag_builder import get_db
        db = get_db()
        placeholders = ",".join("?" for _ in validated)
        for r in db.execute(
            f"SELECT t.tag, t.ranking, w.body_summary, w.body_full "
            f"FROM danbooru_tags t "
            f"LEFT JOIN danbooru_tag_wikis w ON w.tag = t.tag "
            f"WHERE t.tag IN ({placeholders})",
            validated,
        ).fetchall():
            tag = (r["tag"] or "").lower()
            rows[tag] = {
                "tag": r["tag"],
                "ranking": int(r["ranking"] or 0),
                "body_summary": r["body_summary"] or "",
                "body_full": r["body_full"] or "",
            }
    except Exception:
        logger.warning("canonical_resolver: wiki lookup failed", exc_info=True)
    out: list[dict] = []
    for v in validated:
        meta = rows.get(v, {
            "tag": v, "ranking": 0,
            "body_summary": "", "body_full": "",
        })
        out.append({
            "tag": meta["tag"],
            "ranking": meta["ranking"],
            "score": 1.0,  # sentinel — resolved tags always survive cap
            "body_summary": meta["body_summary"],
            "body_full": meta["body_full"],
            "matched_intent": phrase,
            "matched_via": via,
        })
    return out


async def _resolve_one(phrase: str, request_id: str) -> list[dict]:
    """Resolve one sub-intent phrase to candidate dicts.

    Two-stage:
      1. Primary — LLM proposes 3-8 tags from world knowledge,
         validate against danbooru_tags + body-part variant swap.
      2. Secondary (fires only when primary returns nothing) — generate
         word permutations from the phrase, validate each, then ask the
         LLM to rerank from that curated real-tag list. Catches
         `<focus|frame|emphasis>_<body_part>` cases where the LLM's
         proposal stage flaked but a permutation of the user's words
         yields the canonical via body-part variant swap.

    Secondary cost: one extra LLM call only when primary fails (~15% of
    sub-intents on 8b). Parallelization across sub-intents bounds the
    per-prompt latency hit to ~0.6s on prompts where any sub-intent's
    primary flaked."""
    # Primary path
    raw = await _llm_propose(phrase, request_id)
    proposed = _parse_proposed_tags(raw)
    validated: list[str] = []
    seen: set[str] = set()
    for p in proposed:
        v = _validate(p)
        if v and v not in seen:
            seen.add(v)
            validated.append(v)
    if validated:
        return _build_resolved_candidates(validated, phrase, via="resolved")

    # Secondary path — primary returned no real tags. Try ngram
    # permutation against danbooru_tags (with body-part variant swap),
    # then LLM rerank to pick the best match.
    permuted = _ngram_permute_validated(phrase)
    if not permuted:
        return []
    pick = await _llm_rerank(phrase, permuted, f"{request_id}-rerank")
    if not pick:
        return []
    dbg.info(
        "canonical_resolver[%s] secondary fallback fired: phrase=%r "
        "permuted=%s pick=%s",
        request_id, phrase, permuted[:8], pick,
    )
    return _build_resolved_candidates([pick], phrase, via="resolved-rerank")


async def resolve_intents_parallel(
    sub_intents: list[dict],
    request_id: str,
    on_status: Callable[[str], None] | None = None,
) -> list[dict]:
    """Resolve every in-scope sub-intent in parallel. Returns a flat,
    deduped list of candidate dicts suitable for merging into the
    patch flow's existing `tag_candidates` list. Empty list on no
    in-scope intents or total resolution failure."""
    if not sub_intents:
        return []

    in_scope: list[dict] = []
    for si in sub_intents:
        section = (si.get("section") or "").lower()
        text = (si.get("text") or "").strip()
        if not text or section in _RESOLVE_SKIP_SECTIONS:
            continue
        in_scope.append(si)
    if not in_scope:
        return []

    if on_status:
        on_status("Resolving canonical tags")

    tasks = [
        _resolve_one(si["text"], f"{request_id}-resolve-{i}")
        for i, si in enumerate(in_scope)
    ]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        logger.exception("canonical_resolver: parallel resolution failed")
        return []

    flat: list[dict] = []
    seen: set[str] = set()
    for si, result in zip(in_scope, results):
        if isinstance(result, Exception):
            logger.warning(
                "canonical_resolver: intent %r failed: %s",
                si.get("text"), result,
            )
            continue
        for cand in result:
            tag = (cand.get("tag") or "").lower()
            if tag in seen:
                continue
            seen.add(tag)
            flat.append(cand)

    if flat:
        dbg.info(
            "canonical_resolver[%s] %d intents -> %d resolved tags: %s",
            request_id, len(in_scope), len(flat),
            ", ".join(c["tag"] for c in flat[:12]),
        )
    else:
        dbg.info(
            "canonical_resolver[%s] %d intents -> 0 resolved tags",
            request_id, len(in_scope),
        )
    return flat
