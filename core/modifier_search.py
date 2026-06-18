"""
Bi-encoder semantic search over slot_modifiers definitions.

Replaces the old "always show all modifiers as [APPLIES]/[SEMANTIC]" dump
that caused the model to parrot rule-text (slot names, action keywords)
back as tag content.

Index shape: each row of slot_modifiers gets a 384-dim embedding from
BAAI/bge-small-en-v1.5 (same model bucket_search uses), CLS-pooled and
L2-normalized so cosine == dot. Index lives in memory; rowcount-based
hot-reload picks up DB schema changes without restart.

Threshold/top_k are calibrated against real prompts (see commit history).
0.65 / top_k=2 catches semantic paraphrases that miss the alias scan
("her soles aimed at the camera", "the foot is pointed at viewer")
while keeping distractors ("girl walking through forest", "holding a
sword") out — they all top out around 0.50-0.58.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from . import _embed_model


logger = logging.getLogger("promptchain.modifier_search")
_dbg = logging.getLogger("promptchain.ai.debug")

_lock = threading.RLock()
_state: dict[str, Any] = {
    "embeddings": None,        # torch.Tensor [N, 384]
    "modifiers": [],           # list[dict] aligned with embeddings
    "fingerprint": None,       # (max_rowid, count) — change → rebuild
}


def _ensure_model_loaded() -> bool:
    """Defers to the shared embed model loader. Returns False on hard
    failures (caller handles the empty-result fallback) — transient
    init-time errors leave the loader's state unset so the next call
    retries naturally."""
    return _embed_model.get() is not None


def _embed(texts: list[str]):
    return _embed_model.embed(texts, batch_size=64)


def _fingerprint() -> tuple:
    from .tag_builder import get_db
    db = get_db()
    try:
        row = db.execute(
            "SELECT COALESCE(MAX(rowid), 0) AS m, COUNT(*) AS c FROM slot_modifiers"
        ).fetchone()
        return (row["m"], row["c"])
    except Exception:
        return (0, 0)


def _embed_text(mod: dict) -> str:
    """Text we feed to bge-small. Definition-only on purpose — calibration
    showed that mixing aliases into the embed shifts the embedding toward
    specific phrasings ("pointing her feet at viewer") and AWAY from the
    underlying concept, hurting recall on paraphrases the curator didn't
    write down. Aliases continue to drive the deterministic word-boundary
    scan; the embedding stays focused on what the modifier *means*.

    Falls back to the canonical tag name (with underscores → spaces) only
    when no definition has been populated yet — better than skipping the
    row entirely."""
    definition = (mod.get("definition") or "").strip()
    if definition:
        return definition
    return (mod.get("canonical_tag") or "").replace("_", " ").strip()


def _rebuild_index() -> None:
    from . import ai_api
    if not _ensure_model_loaded():
        return
    modifiers = ai_api._load_slot_modifiers()
    if not modifiers:
        _state["embeddings"] = None
        _state["modifiers"] = []
        _state["fingerprint"] = _fingerprint()
        return
    texts = [_embed_text(m) for m in modifiers]
    embeddings = _embed(texts)
    _state["embeddings"] = embeddings
    _state["modifiers"] = list(modifiers)
    _state["fingerprint"] = _fingerprint()
    logger.info("modifier_search: indexed %d modifiers", len(modifiers))


def _ensure_index_fresh() -> None:
    if _state["fingerprint"] == _fingerprint() and _state["embeddings"] is not None:
        return
    _rebuild_index()


def search(user_text: str, top_k: int = 2, threshold: float = 0.65) -> list[dict]:
    """Return modifiers whose definition cosine-matches user_text above
    threshold, sorted by score desc, capped at top_k.

    Each entry is the full modifier dict (from _load_slot_modifiers)
    plus a `score` field. Empty list when the model failed to load —
    caller should degrade to alias-only behavior.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return []
    with _lock:
        if not _ensure_model_loaded():
            return []
        _ensure_index_fresh()
        if _state["embeddings"] is None or not _state["modifiers"]:
            return []
        qv = _embed([user_text])
        scores = (_state["embeddings"] @ qv.T).squeeze(-1).tolist()
        paired = list(zip(_state["modifiers"], scores))
        paired.sort(key=lambda x: -x[1])
        out: list[dict] = []
        for mod, score in paired[:top_k]:
            if score < threshold:
                break
            entry = dict(mod)
            entry["score"] = float(score)
            out.append(entry)
        return out


def warmup() -> None:
    """Best-effort eager load on server boot."""
    import time
    for attempt in range(3):
        with _lock:
            if _ensure_model_loaded():
                _ensure_index_fresh()
                return
            if _embed_model.get_load_error():
                return
        time.sleep(2.0 * (attempt + 1))
