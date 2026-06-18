"""
Style alias scan — curator-authored deterministic literal scan that
maps a user's phrasing in a patch request ("make it anime", "switch to
photography") to a canonical prompt-template id from `data/prompts/`.

Mirrors `tag_search.alias_scan`: same word-boundary regex shape, same
longest-first ordering, same INSERT-OR-IGNORE seed sync semantics, same
"alias hit always beats semantic" sentinel score. Diverges only in
output (template_id, not Danbooru tag) and in arch-filtering (a template
is only a valid hit if it exists in the current architecture's prompt
list — `flux-photography` doesn't fire on a Pony run).

This module deliberately has NO embedding dependency. It loads in
cold-boot, before the bge-small model is online, so the AI Assistant
can detect a style intent on the very first request after a server
restart.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("promptchain.style_search")
_dbg = logging.getLogger("promptchain.ai.debug")

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "tag-builder" / "tag-builder.db"
STYLE_ALIASES_SEED_PATH = (
    REPO_ROOT / "data" / "tag-builder" / "style-aliases-seed.json"
)

# Synthetic template id meaning "the user explicitly asked for no style".
# Not a real entry in data/prompts/; the patch flow uses this signal to
# suppress build-mode default-style injection.
NEUTRAL_TEMPLATE_ID = "_neutral"

# Synthetic template id meaning "use this model's default_prompt_id".
# Resolves to a real template at request time via grounding so we don't
# need per-arch aliases for "default style" / "default look" — works on
# any model that has a default_prompt_id configured.
DEFAULT_TEMPLATE_ID = "_default"

# Synthetic ids that always pass the valid_template_ids check — they
# resolve via downstream lookup (grounding) rather than the prompts list.
_SYNTHETIC_TEMPLATE_IDS = frozenset({NEUTRAL_TEMPLATE_ID, DEFAULT_TEMPLATE_ID})

_lock = threading.RLock()
_ALIAS_LOOKUP_CACHE: list[tuple[str, str]] | None = None


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


def _ensure_style_aliases_schema(conn: sqlite3.Connection) -> None:
    """Create the style_aliases table if missing and sync seed entries
    from JSON every boot. INSERT OR IGNORE keeps curator additions in
    the DB intact — only new seed entries are added."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS style_aliases (
        template_id TEXT NOT NULL,
        alias       TEXT NOT NULL,
        source      TEXT DEFAULT 'curator',
        PRIMARY KEY (template_id, alias)
    );
    CREATE INDEX IF NOT EXISTS idx_style_aliases_template
        ON style_aliases(template_id);
    """)
    conn.commit()
    if not STYLE_ALIASES_SEED_PATH.exists():
        return
    try:
        seed = json.loads(STYLE_ALIASES_SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("style_aliases: seed read failed", exc_info=True)
        return
    # Build the canonical (template_id, alias) set from JSON.
    current_pairs: set[tuple[str, str]] = set()
    for template_id, aliases in seed.items():
        if (template_id.startswith("_")
                and template_id not in _SYNTHETIC_TEMPLATE_IDS):
            continue
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            alias_norm = (alias or "").strip().lower()
            if alias_norm:
                current_pairs.add((template_id.strip(), alias_norm))

    # Insert any new seed pairs.
    inserted = 0
    for tid, alias in current_pairs:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO style_aliases "
                "(template_id, alias, source) VALUES (?, ?, 'seed')",
                (tid, alias),
            )
            if cur.rowcount > 0:
                inserted += 1
        except Exception:
            pass

    # Prune seed-sourced rows no longer in JSON. Curator additions
    # (source != 'seed') are never touched — manual edits via SQL or
    # a future curator UI persist across reboots.
    pruned = 0
    existing = conn.execute(
        "SELECT template_id, alias FROM style_aliases WHERE source = 'seed'"
    ).fetchall()
    for r in existing:
        pair = ((r["template_id"] or "").strip(), (r["alias"] or "").strip().lower())
        if pair not in current_pairs:
            conn.execute(
                "DELETE FROM style_aliases "
                "WHERE source = 'seed' AND template_id = ? AND alias = ?",
                pair,
            )
            pruned += 1

    conn.commit()
    if inserted or pruned:
        logger.info(
            "style_aliases: synced %d new, pruned %d stale seed entries from %s",
            inserted, pruned, STYLE_ALIASES_SEED_PATH.name,
        )


def _load_aliases_by_template_id() -> dict[str, list[str]]:
    """Pull every alias keyed by canonical template_id. Empty dict on
    DB failure — alias scan returns no hits, downstream logic treats
    the request as no-style-intent (which is the safe default)."""
    out: dict[str, list[str]] = {}
    try:
        conn = _open_db()
        _ensure_style_aliases_schema(conn)
        for r in conn.execute(
            "SELECT template_id, alias FROM style_aliases"
        ).fetchall():
            tid = (r["template_id"] or "").strip()
            alias = (r["alias"] or "").strip().lower()
            if tid and alias:
                out.setdefault(tid, []).append(alias)
        conn.close()
    except Exception:
        logger.warning("style_aliases: load failed", exc_info=True)
        return {}
    return out


def _alias_lookup_list() -> list[tuple[str, str]]:
    """Flatten the per-template alias map into a single list of
    (alias, template_id) sorted by alias length descending. Length-
    descending matters: 'anime version of cammy' should match the
    longer 'anime version' first, not bare 'anime', so curator-tuned
    specificity wins. Cached at module level."""
    global _ALIAS_LOOKUP_CACHE
    with _lock:
        if _ALIAS_LOOKUP_CACHE is not None:
            return _ALIAS_LOOKUP_CACHE
        pairs: list[tuple[str, str]] = []
        for tid, aliases in _load_aliases_by_template_id().items():
            for alias in aliases:
                pairs.append((alias, tid))
        pairs.sort(key=lambda p: -len(p[0]))
        _ALIAS_LOOKUP_CACHE = pairs
        return pairs


def invalidate_cache() -> None:
    """Drop the module-level alias lookup cache. Call after writing new
    rows to style_aliases (e.g. via a curator UI)."""
    global _ALIAS_LOOKUP_CACHE
    with _lock:
        _ALIAS_LOOKUP_CACHE = None


def style_alias_scan(user_text: str,
                     valid_template_ids: set[str] | None) -> dict | None:
    """Single-hit scan: return the longest matching alias whose
    template_id is in `valid_template_ids`. Caller is responsible for
    resolving that set via `prompts.list_prompts(...)` with all the
    scope params (architecture / family / model_name / model_hash) —
    model-scoped and family-scoped templates fail to resolve when only
    architecture is supplied, so the caller has to do the full lookup.

    NEUTRAL_TEMPLATE_ID always passes through regardless of the set —
    it's an intent signal, not a template, and used by patch flow to
    suppress build-mode default-style injection.

    Returns None when no alias matches OR every match's template_id is
    out of scope.

    Output: {"template_id", "matched_alias", "is_neutral"}.
    """
    text = (user_text or "").lower()
    if not text:
        return None
    pairs = _alias_lookup_list()
    if not pairs:
        return None

    valid_ids = valid_template_ids or set()

    for alias, tid in pairs:
        # Whitespace/punctuation boundary on both sides. Stops 'anime' from
        # matching inside 'anime convention'. re.escape keeps alias literal.
        pat = re.compile(
            r"(?<![A-Za-z0-9_])"
            + re.escape(alias)
            + r"(?![A-Za-z0-9_])"
        )
        if not pat.search(text):
            continue
        is_synthetic = tid in _SYNTHETIC_TEMPLATE_IDS
        if not is_synthetic and tid not in valid_ids:
            # Alias matched but template isn't a real prompt for this
            # caller's scope. Keep scanning — a different
            # (scope-correct) template may share the alias.
            continue
        return {
            "template_id": tid,
            "matched_alias": alias,
            "is_neutral": tid == NEUTRAL_TEMPLATE_ID,
            "is_default": tid == DEFAULT_TEMPLATE_ID,
        }
    return None


def warmup() -> None:
    """Pre-seed the schema and warm the lookup cache so the first patch
    request after boot doesn't pay the seed-sync cost. Call from
    __init__'s preload daemon thread alongside tag_search.warmup()."""
    try:
        conn = _open_db()
        _ensure_style_aliases_schema(conn)
        conn.close()
        _alias_lookup_list()
    except Exception:
        logger.warning("style_search: warmup failed", exc_info=True)
