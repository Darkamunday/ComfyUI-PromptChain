"""Inline-wildcard population — deterministic node filler.

Turns a structured filter ("street fighter characters with blonde hair",
"all anime styles") into labeled inline-wildcard entries appended to a
node's current content. Format is the `::Label::body` labeled-variant
syntax the compiler resolves in switch / roll mode (see core/compiler.py
resolve_wildcards): newline-separated entries, each body optionally
carrying its own `Negative Prompt:` block, runs until the next `::Label::`.

This path is INTENTIONALLY separate from the AI patch / natlang / compose
pipeline. There is no LLM rewriting here — the chat agent extracts the
structured filter as tool args, and this module does a pure DB / template
query plus deterministic formatting. So it cannot mangle wildcards and
result size is free (one query, no per-item model work).

Non-destructive: existing entries are preserved, new entries appended,
duplicates skipped by label (case-insensitive).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

# Character attribute columns that map directly to a structured filter.
# Anything outside this set ("wields a sword") would need a base_tags
# text-search fallback — deliberately out of scope for v1.
_CHAR_FILTER_COLUMNS = (
    "series", "hair_color", "eye_color", "hair_style",
    "body_type", "breast_size", "ass_size",
)

# Gender isn't a column — it's derivable from the 1girl/1boy count tag in
# base_tags. Maps a gender word (from a `gender` filter OR mistakenly
# placed in body_type) to the tag to match.
_GENDER_TAG = {
    "female": "1girl", "woman": "1girl", "women": "1girl",
    "girl": "1girl", "girls": "1girl", "f": "1girl",
    "male": "1boy", "man": "1boy", "men": "1boy",
    "boy": "1boy", "boys": "1boy", "m": "1boy",
}

# Tokens stripped when synthesizing a body from danbooru tags (the
# count/meta tags and the weighted character-name tag aren't appearance).
_TAG_META = {"1girl", "1boy", "solo", "2girls", "multiple_girls"}
_WEIGHTED_NAME_RE = re.compile(r"^\([^:]+:[\d.]+\)$")
_LABEL_RE = re.compile(r"^::([^:]+)::", re.MULTILINE)


def _default_db_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "tag-builder", "tag-builder.db",
    )


def _char_body_from_tags(base_tags: str, chip_tags: str) -> str:
    """Synthesize a natlang-ish body from danbooru tags for characters
    whose base_natlang is too thin to be useful. Prefer the clean
    appearance_chip_tags JSON array; fall back to splitting base_tags."""
    tags: list[str] = []
    if chip_tags:
        try:
            parsed = json.loads(chip_tags)
            if isinstance(parsed, list):
                tags = [str(t) for t in parsed]
        except Exception:
            tags = []
    if not tags and base_tags:
        tags = [t.strip() for t in base_tags.split(",") if t.strip()]
    out: list[str] = []
    for t in tags:
        t = t.strip()
        if not t or t.lower() in _TAG_META or _WEIGHTED_NAME_RE.match(t):
            continue
        out.append(t.replace("_", " "))
    return ", ".join(out)


def _char_body(row: sqlite3.Row, fmt: str = "natlang") -> str:
    """Body text for a character entry, in the node's format.

    fmt='tags'   -> the character's base_tags verbatim (complete + uniform
                    across the whole roster: name tag + 1girl + appearance).
    fmt='natlang'-> rich base_natlang when present; for thin name-stub bios
                    (e.g. "Cammy White from Street Fighter.") prefix that
                    stub onto tag-synthesized appearance so it reads like
                    the curated prose entries instead of a bare tag list."""
    if fmt == "tags":
        tags = (row["base_tags"] or "").strip()
        if tags:
            return tags
        # No base_tags (rare) — fall through to natlang so we emit something.
    natlang = (row["base_natlang"] or "").strip()
    if len(natlang) > 40:
        return natlang
    synth = _char_body_from_tags(
        row["base_tags"] or "", row["appearance_chip_tags"] or "",
    )
    if natlang and synth:
        # "Cammy White from Street Fighter." + synth appearance, so thin
        # entries read like the curated prose ones (Name from Series, ...).
        return f"{natlang.rstrip('.')}, {synth}"
    return synth or natlang


def _query_characters(db_path: str, filters: dict,
                      *, include_outfit: bool = False,
                      fmt: str = "natlang") -> list[tuple[str, str, str]]:
    """Return [(label, body, negatives), ...] for matching characters.
    Characters carry no per-option negatives, so negatives is always ''.

    include_outfit: append each character's DEFAULT outfit (is_default=1
    from the outfits table) to the appearance body, for requests like
    'the street fighter characters WITH their default outfits'."""
    where = ["base_natlang IS NOT NULL OR base_tags IS NOT NULL"]
    params: list[str] = []

    # Resolve gender from an explicit filter, OR defensively from a gender
    # word the agent mistakenly stuffed into body_type (there is no gender
    # column, and body_type values are builds like 'muscular'/'curvy', so
    # body_type='female' would otherwise match zero rows).
    gender = (filters.get("gender") or filters.get("sex") or "").strip().lower()
    body_type_val = (filters.get("body_type") or "").strip().lower()
    skip_body_type = False
    if not gender and body_type_val in _GENDER_TAG:
        gender = body_type_val
        skip_body_type = True

    for col in _CHAR_FILTER_COLUMNS:
        if col == "body_type" and skip_body_type:
            continue
        val = (filters.get(col) or "").strip()
        if not val:
            continue
        where.append(f"LOWER({col}) LIKE ?")
        params.append(f"%{val.lower()}%")

    if gender in _GENDER_TAG:
        where.append("base_tags LIKE ?")
        params.append(f"%{_GENDER_TAG[gender]}%")
    # LEFT JOIN the default outfit (is_default=1) so it's one query; the
    # join is harmless when include_outfit is False (we just ignore it).
    sql = (
        "SELECT c.tag, c.display, c.base_natlang, c.base_tags, "
        "c.appearance_chip_tags, o.outfit_natlang, o.outfit_tags "
        "FROM characters c "
        "LEFT JOIN outfits o ON LOWER(o.character_tag) = LOWER(c.tag) "
        "AND o.is_default = 1 "
        # characters/outfits share no column names, so the WHERE clauses
        # (base_natlang, series, hair_color, base_tags, ...) are unambiguous.
        "WHERE " + " AND ".join(f"({w})" for w in where)
        + " ORDER BY c.display"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[tuple[str, str, str]] = []
    for r in rows:
        label = (r["display"] or "").strip()
        if not label:
            continue
        body = _char_body(r, fmt)
        if not body:
            continue
        if include_outfit:
            if fmt == "tags":
                outfit = (r["outfit_tags"] or "").strip()
                if outfit:
                    body = f"{body}, {outfit}"
            else:
                outfit = (r["outfit_natlang"] or "").strip()
                if outfit:
                    body = f"{body}, wearing {outfit}"
        out.append((label, body, ""))
    return out


def _query_styles(filters: dict, model_hash: str,
                  grounding: dict | None = None) -> list[tuple[str, str, str]]:
    """Return [(label, positive_body, negatives), ...] for matching style
    templates, scoped to the model's architecture. Lazy imports keep this
    module importable without the server shim until styles are requested.

    `grounding`: pass when the caller already resolved arch/family (the CLI
    harness needs this — `_build_grounding` can't read the model index
    without folder_paths). Production omits it and we compute it."""
    from . import prompts as _prompts
    from .ai_api import _build_grounding, _parse_style_template_text
    if grounding is None:
        grounding = _build_grounding(model_hash) if model_hash else {}
    arch = (grounding.get("architecture") or "").strip() or None
    family = (grounding.get("family") or "").strip() or None
    templates = _prompts.list_prompts(
        architecture=arch, family=family, model_hash=model_hash or None,
    )
    category = (filters.get("category") or "").strip().lower()
    out: list[tuple[str, str, str]] = []
    for t in templates:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if category and category not in (t.get("category") or "").lower():
            continue
        pos, neg = _parse_style_template_text(t.get("text") or "")
        if not pos:
            continue
        out.append((name, ", ".join(pos), ", ".join(neg)))
    return out


def _format_entry(label: str, body: str, negatives: str = "") -> str:
    entry = f"::{label}::{body}"
    if negatives:
        entry += f"\nNegative Prompt:\n{negatives}"
    return entry


def _existing_labels(content: str) -> set[str]:
    return {m.strip().lower() for m in _LABEL_RE.findall(content or "")}


def populate(
    source: str,
    existing_content: str = "",
    *,
    filters: dict | None = None,
    model_hash: str = "",
    db_path: str | None = None,
    grounding: dict | None = None,
    include_outfit: bool = False,
    fmt: str = "natlang",
) -> dict:
    """Append filtered inline-wildcard entries to a node's content.

    source: "characters" | "styles"
    filters: structured filter dict (series/hair_color/.../category)
    Returns {content, summary, added, skipped, total_entries, source}.
    On no matches, content is returned unchanged with an explanatory
    summary so the agent can tell the user nothing matched."""
    filters = filters or {}
    src = (source or "").strip().lower()
    if src in ("characters", "character", "char"):
        entries = _query_characters(db_path or _default_db_path(), filters,
                                    include_outfit=include_outfit, fmt=fmt)
        kind = "character"
    elif src in ("styles", "style"):
        entries = _query_styles(filters, model_hash, grounding)
        kind = "style"
    else:
        return {
            "content": existing_content, "summary": f"Unknown source {source!r}.",
            "added": [], "skipped": [], "total_entries": 0, "source": source,
        }

    existing = existing_content or ""
    seen = _existing_labels(existing)
    added: list[str] = []
    skipped: list[str] = []
    new_blocks: list[str] = []
    for label, body, negatives in entries:
        if label.lower() in seen:
            skipped.append(label)
            continue
        seen.add(label.lower())
        new_blocks.append(_format_entry(label, body, negatives))
        added.append(label)

    if not new_blocks:
        if entries:
            summary = (
                f"All {len(entries)} matching {kind} entries are already in "
                "the node — nothing to add."
            )
        else:
            filt = ", ".join(f"{k}={v}" for k, v in filters.items() if v) \
                or "(no filter)"
            summary = f"No {kind} entries matched {filt}."
        return {
            "content": existing, "summary": summary, "added": [],
            "skipped": skipped, "total_entries": len(seen), "source": source,
        }

    appended = "\n".join(new_blocks)
    content = (existing.rstrip() + "\n" + appended) if existing.strip() else appended

    preview = ", ".join(added[:8]) + (
        f", +{len(added) - 8} more" if len(added) > 8 else ""
    )
    summary = (
        f"Appended {len(added)} {kind} {'entry' if len(added) == 1 else 'entries'} "
        f"as inline wildcards ({preview}); node now has {len(seen)} entries."
    )
    if skipped:
        summary += f" Skipped {len(skipped)} already present."
    return {
        "content": content, "summary": summary, "added": added,
        "skipped": skipped, "total_entries": len(seen), "source": source,
    }
