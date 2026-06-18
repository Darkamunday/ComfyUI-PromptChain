"""Generic KB list — fill a node with inline wildcards of ANY kind.

The KB's eight `*_items` tables share one identical schema
(item_tag, item_group, display_name, base_tags, base_natlang, ...), so a
single catalog-driven resolver covers every flat item domain — clothing,
footwear, poses, expressions, scenes, actions, appearance — instead of a
bespoke tool per noun. New tables/groups are picked up automatically (the
catalog is introspected from the DB), so adding KB content needs no code.

Pipeline: a free-text category phrase ("shoes", "lingerie", "angry
expressions", "rooftop backgrounds") -> deterministic resolve to
(table, group) -> sample N -> emit `::Label::body`. The chat agent's only
job is passing the user's category words; the schema-knowledge lives here.

Characters (attribute/gender/outfit-rich), styles (model-scoped), and
generate_subjects (generative) stay separate — they aren't flat item lists.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3

from .inline_wildcard_populate import (
    _default_db_path, _existing_labels, _format_entry,
)

# Semantic-gap synonyms: user words that don't string-match a group name.
# Most categories (lingerie, swimwear, footwear, anger, lighting, weather,
# location, background, combat, gesture, ...) match a group name directly
# and need no entry here — keep this small and only for true gaps.
# Maps a word -> (table, group | None). group=None means the whole domain.
_SYNONYMS: dict[str, tuple[str, str | None]] = {
    "shoe": ("clothing_items", "footwear"),
    "shoes": ("clothing_items", "footwear"),
    "boot": ("clothing_items", "footwear"),
    "boots": ("clothing_items", "footwear"),
    "heel": ("clothing_items", "footwear"),
    "heels": ("clothing_items", "footwear"),
    "sandal": ("clothing_items", "footwear"),
    "sandals": ("clothing_items", "footwear"),
    "outfit": ("clothing_items", "full_outfits"),
    "outfits": ("clothing_items", "full_outfits"),
    "clothes": ("clothing_items", None),
    "clothing": ("clothing_items", None),
    "bikini": ("clothing_items", "swimwear"),
    "swimsuit": ("clothing_items", "swimwear"),
    "hat": ("clothing_items", "headwear"),
    "hats": ("clothing_items", "headwear"),
    "glasses": ("clothing_items", "accessories"),
    "face": ("expression_items", None),
    "faces": ("expression_items", None),
    "expression": ("expression_items", None),
    "expressions": ("expression_items", None),
    "emotion": ("expression_items", None),
    "emotions": ("expression_items", None),
    "feeling": ("expression_items", None),
    "feelings": ("expression_items", None),
    "pose": ("pose_items", None),
    "poses": ("pose_items", None),
    "action": ("action_items", None),
    "actions": ("action_items", None),
    "background": ("scene_items", "background"),
    "backgrounds": ("scene_items", "background"),
    "setting": ("scene_items", "location"),
    "settings": ("scene_items", "location"),
    "scene": ("scene_items", None),
    "scenes": ("scene_items", None),
}

# Domains excluded from the catalog by default (explicit-only).
_NSFW_TABLES = {"nsfw_action_items"}

# Tables bucket_search semantically indexes. A descriptive query whose
# domain is NOT one of these (clothing, appearance, cast) can't be served
# by bucket_search -> route straight to synth.
_BUCKET_DOMAINS = {"pose_items", "scene_items", "expression_items",
                   "action_items", "nsfw_action_items"}


def _domain_of(table: str) -> str:
    return table[:-len("_items")] if table.endswith("_items") else table


def build_catalog(conn: sqlite3.Connection,
                  *, include_nsfw: bool = False) -> list[dict]:
    """Introspect the DB into a flat catalog of (domain, table, group).
    Auto-discovers every *_items table and its groups — zero maintenance."""
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%\\_items' ESCAPE '\\' ORDER BY name")
    ]
    catalog: list[dict] = []
    for t in tables:
        if t in _NSFW_TABLES and not include_nsfw:
            continue
        groups = [r[0] for r in conn.execute(
            f'SELECT DISTINCT item_group FROM "{t}" '
            "WHERE item_group IS NOT NULL AND item_group != ''")]
        for g in groups:
            catalog.append({"domain": _domain_of(t), "table": t, "group": g})
    return catalog


def _sing(x: str) -> str:
    return x[:-1] if x.endswith("s") and len(x) > 3 else x


# Filler words dropped when extracting search terms from a phrase whose
# qualifier doesn't name a group (e.g. "foot fetish poses" -> [foot, fetish]).
_STOP = {
    "a", "an", "the", "of", "that", "is", "are", "one", "ones", "some",
    "type", "types", "kind", "kinds", "sort", "sorts", "list", "inline",
    "wildcard", "wildcards", "format", "thing", "things", "fetish", "style",
    "in", "for", "with", "and", "me", "give", "show", "add", "bunch",
    "at", "on", "to", "as", "or", "by", "it", "its", "her", "his",
    # category-suffix words: "lingerie OUTFITS" / "cat POSES" name the
    # category, they aren't descriptive content for the multi-word gate.
    "outfit", "outfits", "pose", "poses", "expression", "expressions",
    "scene", "scenes", "action", "actions", "item", "items", "look",
    "looks", "variation", "variations", "option", "options",
}
# Tiny morphological expansion so a search term also hits its variants.
_SEARCH_EXPAND = {"foot": ["feet"], "feet": ["foot"], "leg": ["legs"],
                  "legs": ["leg"], "hand": ["hands"], "hands": ["hand"]}


def resolve_category(conn: sqlite3.Connection, what: str,
                     *, include_nsfw: bool = False) -> tuple:
    """Map a free-text category phrase to (table, group_or_None,
    search_terms_or_None, note). note is non-empty only on failure.
    search_terms is set when a domain matched but the qualifier names no
    group — the caller content-searches the domain's items for those terms
    ("foot fetish poses" -> pose table, no group, search [foot, feet]).
    Strategy, most→least specific:
      1. whole-phrase: synonym / exact group / exact domain
      2. domain + qualifier: scope the qualifier to that domain's groups
         (fuzzy: "angry expressions" -> expression/anger); if no group,
         emit search terms from the leftover qualifier tokens
      3. per-token synonym / exact group (any domain)
      4. substring group, then fuzzy group
    """
    import difflib
    import re as _re
    w = (what or "").strip().lower()
    if not w:
        return (None, None, None, "no category given")
    tokens = [t for t in _re.split(r"\W+", w) if t]
    catalog = build_catalog(conn, include_nsfw=include_nsfw)
    all_groups = {c["group"].lower(): c["table"] for c in catalog}
    domains = {c["domain"].lower(): c["table"] for c in catalog}
    groups_by_table: dict[str, set] = {}
    for c in catalog:
        groups_by_table.setdefault(c["table"], set()).add(c["group"].lower())

    def _syn(x):
        return _SYNONYMS.get(x) or _SYNONYMS.get(_sing(x))

    def _search_terms(domain_tok: str) -> list[str]:
        # Keep descriptive tokens (legs, up, presenting, focus, bare, ...)
        # for relevance ranking; only drop true filler + the domain word.
        terms: list[str] = []
        for t in tokens:
            if t == domain_tok or _sing(t) == _sing(domain_tok):
                continue
            if t in _STOP or _sing(t) in _STOP or len(t) < 2:
                continue
            terms.append(t)
            terms.extend(_SEARCH_EXPAND.get(t, _SEARCH_EXPAND.get(_sing(t), [])))
        return terms or None

    # 1. whole-phrase exact
    for key in (w, _sing(w)):
        if key in _SYNONYMS:
            return (*_SYNONYMS[key], None, "")
        if key in all_groups:
            return (all_groups[key], key, None, "")
        if key in domains:
            return (domains[key], None, None, "")

    # 2. domain + qualifier
    domain_table = None
    domain_tok = ""
    for tok in tokens:
        s = _syn(tok)
        if s and s[1] is None:
            domain_table, domain_tok = s[0], tok
            break
        if tok in domains or _sing(tok) in domains:
            domain_table = domains.get(tok) or domains.get(_sing(tok))
            domain_tok = tok
            break
    if domain_table:
        dgroups = groups_by_table.get(domain_table, set())
        terms = _search_terms(domain_tok) or []
        # A rich description (3+ qualifier tokens) is a SEARCH, even if one
        # token happens to be a group name ("legs up presenting feet" must
        # not collapse to the 'legs' group and miss 'presenting_feet').
        # A short qualifier (1-2 tokens) is a category -> match a group.
        distinct = [t for t in terms if t not in _SEARCH_EXPAND]
        if len(distinct) < 3:
            for tok in tokens:
                ts = _sing(tok)
                if tok in dgroups or ts in dgroups:
                    return (domain_table, tok if tok in dgroups else ts,
                            None, "")
                for g in dgroups:
                    if ts and len(ts) >= 3 and (ts in g or g in ts):
                        return (domain_table, g, None, "")
                cl = difflib.get_close_matches(ts, list(dgroups), n=1,
                                               cutoff=0.75)
                if cl:
                    return (domain_table, cl[0], None, "")
        # descriptive, or no group matched -> search the domain.
        return (domain_table, None, terms or None, "")

    # Per-token category matching (steps 3-4) only for a SHORT phrase. A
    # multi-word description ("pink micro bikini", "red lace-up boots") must
    # NOT collapse to a group just because one token ("bikini") names one —
    # that's a described item to retrieve/synthesize, not a category dump.
    content = [t for t in tokens if t not in _STOP and len(t) >= 3]
    if len(content) >= 2:
        # Descriptive -> semantic/synth. Report a domain HINT (which table
        # the description belongs to) so the caller routes correctly:
        # pose/scene/expr/action -> bucket_search; clothing/appearance
        # (not in the semantic index) -> synthesize directly.
        hint = None
        hint_nonbucket = None
        for tok in tokens:
            s = _syn(tok)
            tbl = None
            if s:
                tbl = s[0]
            elif tok in all_groups:
                tbl = all_groups[tok]
            elif tok in domains or _sing(tok) in domains:
                tbl = domains.get(tok) or domains.get(_sing(tok))
            if not tbl:
                continue
            if hint is None:
                hint = tbl
            # Prefer a clothing/appearance (non-bucket) domain — bucket_search
            # can't serve it, and a garment noun ("combat boots") should win
            # over an incidental bucket-domain modifier ("combat").
            if tbl not in _BUCKET_DOMAINS and hint_nonbucket is None:
                hint_nonbucket = tbl
        return (hint_nonbucket or hint, None, None, "")  # descriptive

    # 3. per-token synonym / exact group
    for tok in tokens:
        s = _syn(tok)
        if s:
            return (*s, None, "")
        ts = _sing(tok)
        if tok in all_groups:
            return (all_groups[tok], tok, None, "")
        if ts in all_groups:
            return (all_groups[ts], ts, None, "")

    # 4. substring group, then fuzzy group
    for c in catalog:
        gl = c["group"].lower()
        if _sing(w) in gl or gl in w:
            return (c["table"], c["group"], None, "")
    close = difflib.get_close_matches(_sing(w), list(all_groups), n=1, cutoff=0.8)
    if close:
        return (all_groups[close[0]], close[0], None, "")

    return (None, None, None, "")


def _rget(row, key: str) -> str:
    try:
        return (row[key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _item_body(row, fmt: str) -> str:
    # tag mode: prefer the full canonical tag set (base_tags), e.g.
    # "legs_up, sitting, presenting_feet, soles, foot_focus"; fall back to
    # the single item_tag. natlang mode: the curated prose description.
    if fmt == "tags":
        return (_rget(row, "base_tags")
                or _rget(row, "item_tag").replace(" ", "_")).strip()
    return (_rget(row, "base_natlang") or _rget(row, "display_name")).strip()


_SEMANTIC_FLOOR = 0.45
_SEMANTIC_DEFAULT_N = 10
# scene-bucket framing groups (camera angle / composition crops). These
# pollute pose/subject queries ("foot focus" -> the 'Foot Focus' crop), so
# demote them UNLESS the user explicitly asks about framing/composition.
_FRAMING_GROUPS = {"camera", "composition"}
_FRAMING_PENALTY = 0.15
_FRAMING_INTENT = ("camera", "composition", "framing", "shot", "angle",
                   "crop", "close-up", "closeup", "wide shot")


def _semantic_items(conn: sqlite3.Connection, what: str,
                    count: int | None) -> tuple[list[dict], str, str]:
    """Semantic top-N retrieval via bucket_search (embeddings over the
    curated pose/scene/expression/action bundles). Returns
    (items, domain, note). Each item is the matched bundle's row so the
    body comes from curated base_tags / base_natlang. This is what makes
    'a pose like <description>' -> presenting_feet and '5 <theme> poses'
    -> 5 distinct on-theme bundles work — semantic, not substring."""
    try:
        from . import bucket_search
    except Exception as e:  # torch/model not available
        return [], "", f"semantic search unavailable: {e}"
    k = count if (count and count > 0) else _SEMANTIC_DEFAULT_N
    try:
        results = bucket_search.search(what, top_k=max(k, 5) + 8)
    except Exception as e:
        return [], "", f"semantic search failed: {e}"
    framing_intent = any(w in (what or "").lower() for w in _FRAMING_INTENT)
    scored: list[tuple[float, dict]] = []
    domain = ""
    for r in results:
        sc = float(r.get("score") or r.get("adjusted_score") or 0)
        if sc < _SEMANTIC_FLOOR:
            continue
        bucket = (r.get("bucket") or "").strip()
        itag = (r.get("item_tag") or "").strip()
        domain = domain or bucket
        base_tags = r.get("base_tags") or ""
        base_natlang = ""
        display = (r.get("display_name") or itag).strip()
        group = ""
        # search() omits base_natlang/item_group — fetch the curated row.
        try:
            row = conn.execute(
                f'SELECT base_natlang, base_tags, display_name, item_group '
                f'FROM "{bucket}_items" WHERE item_tag = ? LIMIT 1',
                (itag,)).fetchone()
            if row:
                base_natlang = row["base_natlang"] or ""
                base_tags = base_tags or (row["base_tags"] or "")
                display = display or (row["display_name"] or "")
                group = (row["item_group"] or "").lower()
        except sqlite3.OperationalError:
            pass  # bucket has no `<bucket>_items` table (e.g. prop) — use entry
        # Demote scene framing crops (camera/composition) on non-framing
        # queries so genuine poses/subjects win ("foot focus" no longer
        # surfaces the 'Foot Focus' camera crop above the pose).
        if (bucket == "scene" and group in _FRAMING_GROUPS
                and not framing_intent):
            sc -= _FRAMING_PENALTY
        scored.append((sc, {"item_tag": itag, "display_name": display,
                            "base_tags": base_tags, "base_natlang": base_natlang}))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Return the full ranked pool (capped) — list_items applies `count`
    # AFTER deduping against the node ("add a second" skips dups).
    return [e for _s, e in scored[:25]], (domain or "kb"), ""


def _synthesize_row(what: str, fmt: str) -> dict | None:
    """Tier-2 fallback: the KB has no curated match for `what`, so compose
    ONE entry from the description — the same 'make it up' capability the
    patch flow uses for 'pink micro bikini'. Tag side: semantic Danbooru
    tag search (tag_search alias_scan + search). Natlang side: the cleaned
    description as prose. Returns a row dict, or None if nothing usable."""
    import re as _re
    toks = [t for t in _re.split(r"\W+", (what or "").lower()) if t]
    content = [t for t in toks if t not in _STOP and len(t) >= 2]
    if not content:
        return None
    label = " ".join(w.capitalize() for w in content[:5])
    core = " ".join(content)
    base_tags = ""
    base_natlang = ""
    if fmt == "tags":
        tags: list[str] = []
        try:
            from . import tag_search
            for h in (tag_search.alias_scan(core) or []):
                tg = h.get("tag") if isinstance(h, dict) else h
                if tg:
                    tags.append(tg)
            for h in (tag_search.search(core, top_k=8, threshold=0.5) or []):
                tg = h.get("tag") if isinstance(h, dict) else h
                if tg:
                    tags.append(tg)
        except Exception:
            tags = []
        seen: set = set()
        tags = [t for t in tags if t and not (t in seen or seen.add(t))]
        base_tags = ", ".join(tags) if tags else "_".join(content)
    else:
        body = (what or core).strip()
        body = _re.sub(r"^(inline\s+wildcard\s+of\s+|a\s+|an\s+|some\s+)", "",
                       body, flags=_re.I).strip()
        base_natlang = body or core
    return {"display_name": label, "item_tag": label.lower().replace(" ", "_"),
            "base_tags": base_tags, "base_natlang": base_natlang}


def list_items(
    what: str,
    *,
    count: int | None = None,
    fmt: str = "natlang",
    existing_content: str = "",
    seed: int | None = None,
    db_path: str | None = None,
    include_nsfw: bool = False,
) -> dict:
    """Fill a node with inline wildcards for `what`.

    Two paths:
      - CATEGORY (clean group: 'shoes'->footwear, 'lingerie', 'anger') ->
        deterministic group dump, `count` random-samples.
      - DESCRIPTIVE / theme ('foot fetish poses', 'sitting legs up
        presenting feet', '5 shy expressions') -> semantic top-N via
        bucket_search; `count` is top_k, results are best-first.
    Emits `::Label::body` (body = curated base_tags / base_natlang per
    `fmt`), appended non-destructively."""
    conn = sqlite3.connect(db_path or _default_db_path())
    conn.row_factory = sqlite3.Row
    semantic = False
    used_group = None
    note = ""
    try:
        table, group, search, note = resolve_category(
            conn, what, include_nsfw=include_nsfw)
        cols = "item_tag, display_name, base_natlang, base_tags"
        if table and group:
            used_group = group
            domain = _domain_of(table)
            rows = conn.execute(
                f'SELECT {cols} FROM "{table}" WHERE LOWER(item_group)=LOWER(?) '
                "ORDER BY sort_order, display_name", (group,)).fetchall()
            items = [dict(r) for r in rows]
        else:
            # No clean category -> descriptive. Decide retrieve vs synth:
            #  - RICH specific description (>=4 content words, e.g.
            #    "presenting a foot while standing, one leg extended, foot
            #    focus") -> SYNTHESIZE: compose the user's full intent into
            #    one entry. Retrieval would only return a PARTIAL KB match
            #    (a balance pose, or a sitting presentation) and miss the
            #    combination.
            #  - SPARSE theme ("foot poses", "shy expressions") -> RETRIEVE
            #    curated variety via bucket_search.
            #  - clothing/appearance (not in the semantic index) -> synth.
            semantic = True
            content_n = len([t for t in re.split(r"\W+", what.lower())
                             if t and t not in _STOP and len(t) >= 3])
            rich = content_n >= 5
            if rich or (table and table not in _BUCKET_DOMAINS):
                items, domain = [], (_domain_of(table) if table else "synth")
            else:
                items, domain, snote = _semantic_items(conn, what, count)
                note = snote or note
            if not items:
                # Tier-2: KB had no curated match -> synthesize one entry
                # from the description (e.g. "pink micro bikini").
                synth = _synthesize_row(what, fmt)
                if synth:
                    items = [synth]
                    domain = "synth"
                    note = f"synthesized (no KB match for {what!r})"
    finally:
        conn.close()

    rng = random.Random(seed if seed is not None else random.randrange(2 ** 31))
    if not semantic and count and 0 < count < len(items):
        # Category dump: sample `count`. (Semantic items are already the
        # ranked top-`count` from bucket_search — never re-sample those.)
        items = rng.sample(items, count)

    seen = _existing_labels(existing_content or "")
    added: list[str] = []
    skipped: list[str] = []
    blocks: list[str] = []
    added_pairs: list[tuple[str, str]] = []
    for it in items:
        label = (it.get("display_name") or it.get("item_tag") or "").strip()
        body = _item_body(it, fmt)
        if not label or not body:
            continue
        if label.lower() in seen:
            skipped.append(label)
            continue
        seen.add(label.lower())
        blocks.append(_format_entry(label, body))
        added.append(label)
        added_pairs.append((label, body))
        # count = number of NEW (non-duplicate) entries; stop once met so
        # "add a second" adds one genuinely-new item, not a dup or nothing.
        if count and len(added) >= count:
            break

    scope = f"{domain}/{used_group}" if used_group else (domain or "kb")
    if not blocks:
        msg = (note or (f"All matching {scope} items are already in the node."
                        if skipped else f"No matches for {what!r}."))
        return {"content": existing_content or "", "summary": msg,
                "added": [], "skipped": skipped, "domain": domain or None,
                "group": used_group, "note": msg}

    appended = "\n".join(blocks)
    existing = existing_content or ""
    content = (existing.rstrip() + "\n" + appended) if existing.strip() else appended
    # Summary carries the ACTUAL entries (label + body) so the agent narrates
    # what really loaded instead of inventing a description. Cap the bodies
    # for large adds to keep the narration context small.
    if len(added_pairs) <= 6:
        listing = "; ".join(f"{lbl} — {bd[:80]}" for lbl, bd in added_pairs)
    else:
        listing = ", ".join(lbl for lbl, _ in added_pairs[:6]) + \
            f", +{len(added_pairs) - 6} more"
    summary = (f"Added {len(added)} {scope} item"
               f"{'' if len(added) == 1 else 's'} as inline wildcards. "
               f"Entries (use these EXACT descriptions, do not invent): "
               f"{listing}. Node now has {len(seen)} entries.")
    return {"content": content, "summary": summary, "added": added,
            "skipped": skipped, "domain": domain or None, "group": used_group,
            "note": note}
