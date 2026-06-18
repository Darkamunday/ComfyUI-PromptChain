"""Resolve probe — named entity → canonical content from tag-builder.db.

Sits between decompose and locate-infill. Takes a decompose intent
(concept, op, raw_text) and tries to map raw_text onto a known entity
in tag-builder.db. If a confident match exists, returns the canonical
natlang composition; otherwise the raw text is passed through.

Concept routing:
  subject                                  → characters table
  tops/bottoms/footwear/.../outfit_swap   → outfits table (per character) or generic_outfits
  scene                                    → scene_items table
  style                                    → style_aliases (TODO — schema is more complex)
  pose                                     → poses table

Matching strategies (cheapest first, no LLM):
  - exact tag match
  - exact display match (case-insensitive)
  - tag normalization (spaces ↔ _ ↔ -, strip parens)
  - first-name prefix (cammy → cammy_white)
  - last resort: substring on display

If still ambiguous, fall back to LLM call to disambiguate. For now
ambiguous = return None and let the caller decide.

Run:
  cd C:/comfyui/comfyui/custom_nodes/ComfyUI-PromptChain
  python scripts/natlang_resolve_probe.py
  python scripts/natlang_resolve_probe.py <filter>
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import types
from typing import Optional


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _StubRoutes:
    def _passthrough(self, _p):
        def w(f): return f
        return w
    post = get = put = delete = patch = head = options = _passthrough


sys.modules.setdefault(
    "folder_paths",
    types.SimpleNamespace(
        folder_names_and_paths={},
        get_folder_paths=lambda x: [],
        get_full_path=lambda *a, **k: None,
        models_dir="/tmp",
        get_user_directory=lambda: os.path.join(ROOT, "..", "..", "user"),
        base_path=os.path.join(ROOT, "..", ".."),
    ),
)
sys.modules.setdefault(
    "server",
    types.SimpleNamespace(PromptServer=types.SimpleNamespace(
        instance=types.SimpleNamespace(routes=_StubRoutes(),
                                       send_sync=lambda *a, **k: None),
    )),
)


DB_PATH = os.path.join(
    ROOT, "data", "tag-builder", "tag-builder.db"
)


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Reuse the existing composers from tag_builder.py.
from core.tag_builder import (  # noqa: E402
    compose_character_natlang_v2,
    compose_outfit_natlang_v2,
    compose_pose_natlang_v2,
)

# Style template resolution mirrors what legacy /ai/patch does:
# style_alias_scan against the user-supplied text returns a matched
# template_id, then we pull the template body via prompts.list_prompts
# and parse it the same way legacy _build_style_section does. Imports
# stay optional so the probe module remains usable in environments
# where the prompts package can't initialize (e.g. CI without user dir).
try:
    from core import style_search as _style_search  # noqa: E402
    from core import prompts as _prompts            # noqa: E402
    from core.ai_api import (  # noqa: E402
        _parse_style_template_text as _parse_style_text,
        _build_grounding as _build_grounding,
    )
except Exception:
    _style_search = None
    _prompts = None
    _parse_style_text = None
    _build_grounding = None


_POSE_FILLER_PATTERN = re.compile(
    r"\b(pose|stance|posture|position|signature|move)\b",
    re.IGNORECASE,
)


def _extract_subject_name(char_sentence: str) -> str:
    """Pull just the `<Name>` portion out of a long character
    description. The natlang format opens with `<Name> from <Series>,
    <trait>, <trait>, ...` — strip everything from the first comma
    onward and strip a trailing ` from <Series>` so match_character
    has a clean display string to compare against.
    """
    s = (char_sentence or "").strip()
    if not s:
        return ""
    head = s.split(",", 1)[0].strip()
    head = re.sub(r"\s+from\s+.+$", "", head, flags=re.IGNORECASE).strip()
    return head


# ── Normalizers ───────────────────────────────────────────────────


def _normalize_token(s: str) -> str:
    """Lowercase, collapse whitespace, drop parens, swap separators."""
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[()'’]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _strip_outfit_words(s: str) -> str:
    """Strip 'outfit/uniform/clothes/outfit of' suffixes."""
    s = (s or "").strip().lower()
    s = re.sub(r"['’]s\b", "", s)
    s = re.sub(r"\b(outfit|clothes|clothing|attire|costume|uniform|getup|garb|fit)s?\b", "", s)
    s = re.sub(r"\b(of|the|her|his|their|in)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── Character match ──────────────────────────────────────────────


def match_character(conn: sqlite3.Connection, text: str) -> Optional[dict]:
    """Return a character row (as dict) or None."""
    if not text:
        return None
    norm = _normalize_token(text)
    raw_lower = text.strip().lower()

    # 1. Exact tag.
    row = conn.execute(
        "SELECT tag, display, series FROM characters WHERE tag = ?",
        (norm,),
    ).fetchone()
    if row:
        return dict(row)

    # 2. Display match (case-insensitive).
    row = conn.execute(
        "SELECT tag, display, series FROM characters "
        "WHERE LOWER(display) = ? LIMIT 1",
        (raw_lower,),
    ).fetchone()
    if row:
        return dict(row)

    # 3. Tag with simple punctuation variants (chun li → chun-li).
    variants = {
        norm,
        norm.replace("_", "-"),
        re.sub(r"_+", "-", norm),
    }
    for v in variants:
        row = conn.execute(
            "SELECT tag, display, series FROM characters WHERE tag = ?",
            (v,),
        ).fetchone()
        if row:
            return dict(row)

    # 4. First-name prefix — "cammy" → cammy_white (and similar).
    if "_" not in norm and len(norm) >= 3:
        rows = conn.execute(
            "SELECT tag, display, series, post_count FROM characters "
            "WHERE tag LIKE ? ORDER BY post_count DESC LIMIT 3",
            (f"{norm}_%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            # Ambiguous prefix — pick highest post_count if it dominates
            # by 5x; otherwise punt.
            if rows[0]["post_count"] >= 5 * (rows[1]["post_count"] or 1):
                return dict(rows[0])
            return None

    # 5. Substring on display (last resort).
    rows = conn.execute(
        "SELECT tag, display, series, post_count FROM characters "
        "WHERE LOWER(display) LIKE ? "
        "ORDER BY post_count DESC LIMIT 3",
        (f"%{raw_lower}%",),
    ).fetchall()
    if rows:
        if len(rows) == 1:
            return dict(rows[0])
        if rows[0]["post_count"] >= 5 * (rows[1]["post_count"] or 1):
            return dict(rows[0])
    return None


# ── Outfit match ────────────────────────────────────────────────


def _match_foreign_character_outfit(conn: sqlite3.Connection,
                                    text: str,
                                    current_character_tag: str) -> Optional[dict]:
    """Detect a cross-character outfit reference like 'chun-li outfit' or
    'chun-li\\'s qipao' when the active character is someone else (e.g.
    cammy_white). Returns the foreign character's outfit row (default
    or named) with `character_display` set to the foreign character so
    the dispatch step can emit a 'from Character: <Foreign>' header."""
    if not text:
        return None
    raw_lower = text.strip().lower()
    # Possessive form: "chun-li's qipao" → ("chun-li", "qipao")
    poss = re.search(r"^(.+?)['’]s\s+(.+)$", raw_lower)
    candidate_char_text: str = ""
    candidate_outfit_name: str = ""
    if poss:
        candidate_char_text = poss.group(1).strip()
        candidate_outfit_name = poss.group(2).strip()
    else:
        # Non-possessive: scan ALL contiguous 1–3 token windows for a
        # character name anywhere in the text. This catches forms like
        # "wearing chun-li clothes" where the character name is
        # mid-phrase, not just at the start.
        stripped = _strip_outfit_words(text)
        tokens = stripped.split()
        found = False
        for start in range(len(tokens)):
            if found:
                break
            for cut in range(min(3, len(tokens) - start), 0, -1):
                head = " ".join(tokens[start:start + cut])
                ch = match_character(conn, head)
                if ch and ch["tag"] != current_character_tag:
                    candidate_char_text = head
                    # outfit-name candidate = tokens OUTSIDE the matched range
                    candidate_outfit_name = " ".join(
                        tokens[:start] + tokens[start + cut:]
                    )
                    found = True
                    break
    if not candidate_char_text:
        return None
    foreign = match_character(conn, candidate_char_text)
    if not foreign or foreign["tag"] == current_character_tag:
        return None
    # Look up the foreign character's outfit. Prefer a named outfit if
    # candidate_outfit_name is non-trivial; otherwise their default.
    cleaned_name = (candidate_outfit_name or "").strip()
    cleaned_name = re.sub(r"\b(outfit|clothes|attire|look|set|gear|costume|getup)\b",
                          "", cleaned_name, flags=re.IGNORECASE).strip()
    if cleaned_name:
        row = conn.execute(
            "SELECT id, character_tag, outfit_name, is_default "
            "FROM outfits "
            "WHERE character_tag = ? AND LOWER(outfit_name) LIKE ? "
            "ORDER BY is_default DESC LIMIT 1",
            (foreign["tag"], f"%{cleaned_name.lower()}%"),
        ).fetchone()
        if row:
            d = dict(row)
            d.update({"outfit_id": d["id"], "source": "outfit",
                      "character_display": foreign["display"],
                      "foreign_character": True})
            return d
        # User named a specific outfit for this foreign character but
        # they don't have it. DO NOT silently fall back to their
        # default outfit (that's how "Chun-Li bikinis" wrongly
        # returned her SF2 Classic qipao). Return None so the caller
        # falls through to generic_outfits / literal-text dispatch.
        return None
    # Only when NO specific outfit was named ("chun-li's outfit",
    # "Cammy's clothes") fall back to the foreign character's default.
    row = conn.execute(
        "SELECT id, character_tag, outfit_name, is_default "
        "FROM outfits WHERE character_tag = ? AND is_default = 1 LIMIT 1",
        (foreign["tag"],),
    ).fetchone()
    if row:
        d = dict(row)
        d.update({"outfit_id": d["id"], "source": "outfit",
                  "character_display": foreign["display"],
                  "foreign_character": True})
        return d
    return None


def match_outfit(conn: sqlite3.Connection, text: str,
                 character_tag: Optional[str] = None) -> Optional[dict]:
    """Find an outfit row by (character + outfit_name) or generic_outfit.
    If character_tag is given, restrict to that character first — UNLESS
    the text references a DIFFERENT character (e.g. 'chun-li outfit'
    while the active character is cammy_white), in which case scope to
    the foreign character.
    Returns dict with at least {char_tag, outfit_id, outfit_name,
    is_default, source} where source is "outfit" or "generic"."""
    if not text:
        return None
    stripped = _strip_outfit_words(text)
    # Cross-character path first: when a different character is named,
    # honor that instead of falling back to the active character's
    # default outfit. Mirrors TagBuilder v2's "X wearing Y's clothes".
    if character_tag:
        foreign = _match_foreign_character_outfit(conn, text, character_tag)
        if foreign:
            return foreign
    # First try: caller-specified character + their default outfit.
    if character_tag:
        # Default-outfit injection from _maybe_inject_default_outfit
        # formats text as "{char_display} {outfit_name}" so the
        # foreign-outfit scan can pick up a different character at the
        # start. When the leading name is the ACTIVE character, the
        # foreign path correctly bails — but the LIKE query below then
        # matches "{char} {outfit}" against outfit_name (which is just
        # "{outfit}") and fails. Strip the active character's display
        # name out of the search text first.
        if stripped:
            try:
                ch_row = conn.execute(
                    "SELECT display FROM characters WHERE tag = ?",
                    (character_tag,),
                ).fetchone()
                active_display = (ch_row["display"] if ch_row else "") or ""
            except Exception:
                active_display = ""
            if active_display:
                ad_lc = active_display.lower()
                if stripped.lower().startswith(ad_lc + " "):
                    stripped = stripped[len(ad_lc):].strip()
                elif stripped.lower() == ad_lc:
                    # Whole text is just the character name — no outfit
                    # named → fall through to default lookup below.
                    stripped = ""
        # If stripped text mentions an outfit name, match it.
        if stripped:
            row = conn.execute(
                "SELECT id, character_tag, outfit_name, is_default "
                "FROM outfits "
                "WHERE character_tag = ? AND LOWER(outfit_name) LIKE ? "
                "ORDER BY is_default DESC LIMIT 1",
                (character_tag, f"%{stripped}%"),
            ).fetchone()
            if row:
                d = dict(row)
                d.update({"outfit_id": d["id"], "source": "outfit"})
                return d
            # User named an outfit but it doesn't exist for this
            # character. Fall through to generic_outfits below — do
            # NOT silently return their default, that would silently
            # ignore the user's text ("bikinis" → wrongly returns
            # Cammy's Delta Red). The generic-outfit lookup at the
            # bottom may catch common clothing types; if it misses
            # too, return None so the composer uses the literal text.
        else:
            # No specific outfit named (text was "her outfit", "his
            # clothes" or similar) — take the character's default.
            row = conn.execute(
                "SELECT id, character_tag, outfit_name, is_default "
                "FROM outfits "
                "WHERE character_tag = ? AND is_default = 1 LIMIT 1",
                (character_tag,),
            ).fetchone()
            if row:
                d = dict(row)
                d.update({"outfit_id": d["id"], "source": "outfit"})
                return d

    # No character context — try: text mentions a character name. Try
    # matching the WHOLE text against characters first, then their
    # default outfit.
    ch = match_character(conn, stripped or text)
    if ch:
        row = conn.execute(
            "SELECT id, character_tag, outfit_name, is_default "
            "FROM outfits WHERE character_tag = ? AND is_default = 1 LIMIT 1",
            (ch["tag"],),
        ).fetchone()
        if row:
            d = dict(row)
            d.update({"outfit_id": d["id"], "source": "outfit",
                      "character_display": ch["display"]})
            return d

    # Fall back to generic_outfits (school uniform, kimono, bikini,
    # etc.). Try exact name, plural-stripped name, and aliases
    # substring so common forms like "bikinis" → "Bikini" match.
    if stripped:
        singular = stripped.rstrip("s") if stripped.endswith("s") else stripped
        row = conn.execute(
            "SELECT id, name, aliases, outfit_natlang FROM generic_outfits "
            "WHERE LOWER(name) = ? "
            "OR LOWER(name) = ? "
            "OR LOWER(name) LIKE ? "
            "OR LOWER(aliases) LIKE ? "
            "LIMIT 1",
            (stripped, singular, f"%{stripped}%", f"%{stripped}%"),
        ).fetchone()
        if row:
            d = dict(row)
            d.update({"source": "generic", "outfit_id": d["id"]})
            return d
    return None


# ── Pose match ────────────────────────────────────────────────


def match_pose(conn: sqlite3.Connection, text: str,
               character_tag: Optional[str] = None) -> Optional[dict]:
    """Match a pose by name. Two shapes:

      "<character>'s <pose name>"   →  parses character + pose name
      "<pose name>"                  →  needs character_tag from caller

    Returns the row dict (with `id` / `pose_id`) or None when no
    confident match.
    """
    if not text:
        return None
    raw = text.strip()
    raw_lower = raw.lower()

    # Possessive form: "cammy white's victory pose" -> ("cammy white", "victory pose")
    poss = re.match(r"^(.+?)['’]s\s+(.+)$", raw_lower)
    pose_name_query = None
    if poss:
        char_part = poss.group(1).strip()
        pose_part = poss.group(2).strip()
        ch = match_character(conn, char_part)
        if ch:
            character_tag = ch["tag"]
        pose_name_query = _POSE_FILLER_PATTERN.sub("", pose_part).strip()
    else:
        # Maybe the text starts with a character name without possessive:
        # "cammy white victory pose"
        ch = match_character(conn, raw_lower)
        if ch:
            character_tag = ch["tag"]
            display_lc = (ch.get("display") or "").lower()
            tag_lc = (ch.get("tag") or "").lower().replace("_", " ")
            tail = raw_lower
            for prefix in (display_lc, tag_lc):
                if prefix and tail.startswith(prefix):
                    tail = tail[len(prefix):].lstrip(" '’s")
                    break
            pose_name_query = _POSE_FILLER_PATTERN.sub("", tail).strip()
        else:
            pose_name_query = _POSE_FILLER_PATTERN.sub("", raw_lower).strip()

    if not character_tag or not pose_name_query:
        return None

    # Word-overlap match on pose_name. The user's `pose_name_query` may
    # be a sub-phrase ("victory") of the DB row's full name
    # ("Victory Pose (Rear)"). Prefer signature poses, then sort_order.
    rows = conn.execute(
        "SELECT id, character_tag, pose_name, is_signature, sort_order "
        "FROM poses WHERE character_tag = ? "
        "ORDER BY is_signature DESC, sort_order ASC",
        (character_tag,),
    ).fetchall()
    if not rows:
        return None
    needle = set(re.findall(r"[a-z0-9]+", pose_name_query))
    if not needle:
        return None
    best = None
    best_score = 0
    for r in rows:
        name_lc = (r["pose_name"] or "").lower()
        haystack = set(re.findall(r"[a-z0-9]+", name_lc))
        score = len(needle & haystack)
        if score > best_score:
            best = r
            best_score = score
    if best is None or best_score == 0:
        return None
    d = dict(best)
    d["pose_id"] = d["id"]
    return d


# ── Style match ────────────────────────────────────────────────


_STYLE_NAME_FALLBACK_THRESHOLD = 4.0


def _style_name_overlap_fallback(text: str,
                                 arch_prompts: list[dict]) -> Optional[dict]:
    """Mirror legacy ai_api's char-weighted name-overlap scoring. When
    the alias scan misses, score every template's name against the
    user's request using overlap_chars² / name_chars (length-weighted),
    pick the best above threshold. Lets "hyperrealistic anime style"
    land on a template named "Hyperrealistic" even when no alias is
    registered for it."""
    user_words = set(re.findall(r"\w{4,}", (text or "").lower()))
    if not user_words:
        return None
    best = None
    best_score = 0.0
    for p in arch_prompts:
        name = (p.get("name") or "").lower()
        name_words = set(re.findall(r"\w{4,}", name))
        if not name_words:
            continue
        overlap = name_words & user_words
        if not overlap:
            continue
        overlap_chars = sum(len(w) for w in overlap)
        name_chars = sum(len(w) for w in name_words)
        if name_chars == 0:
            continue
        adj = (overlap_chars ** 2) / name_chars
        if adj > best_score:
            best = p
            best_score = adj
    if best and best_score >= _STYLE_NAME_FALLBACK_THRESHOLD:
        return best
    return None


def match_style_template(text: str,
                         model_hash: Optional[str] = None,
                         architecture: Optional[str] = None) -> Optional[dict]:
    """Resolve a style template the same way legacy /ai/patch does.

    1. Derive architecture from model_hash (when given) via
       _build_grounding, so the valid template pool only contains
       templates compatible with the active checkpoint.
    2. Run style_alias_scan against the scoped pool. Aliases sorted
       longest-first; templates outside the pool get skipped.
    3. On alias miss, fall back to char-weighted name overlap scoring
       so requests like "hyperrealistic anime style" find a template
       named "Hyperrealistic" even without an alias registered.

    Output: {"template_id", "name", "body_text", "neg_tokens",
             "matched_alias", "is_default", "via"} or None.

    `via` is "alias" or "name_overlap" so the caller can tell which
    path resolved the template (useful in logs and the harness).
    """
    if _style_search is None or _prompts is None or _parse_style_text is None:
        return None
    if not (text or "").strip():
        return None

    # Derive arch from model_hash when caller didn't pass it explicitly.
    if architecture is None and model_hash and _build_grounding is not None:
        try:
            grounding = _build_grounding(model_hash)
            architecture = (grounding.get("architecture") or "").strip() or None
        except Exception:
            architecture = None

    try:
        arch_prompts = _prompts.list_prompts(
            architecture=architecture,
            model_hash=model_hash,
        )
    except Exception:
        return None
    valid_ids = {(p.get("id") or "").strip()
                 for p in arch_prompts if p.get("id")}

    template = None
    matched_alias = None
    via = None

    # Alias scan first (curator-tuned, highest specificity).
    try:
        hit = _style_search.style_alias_scan(text, valid_ids)
    except Exception:
        hit = None
    if hit and not hit.get("is_neutral"):
        template_id = hit.get("template_id")
        for p in arch_prompts:
            if (p.get("id") or "").strip() == template_id:
                template = p
                matched_alias = hit.get("matched_alias")
                via = "alias"
                break

    # Name word-overlap fallback when alias didn't resolve.
    if template is None:
        template = _style_name_overlap_fallback(text, arch_prompts)
        if template is not None:
            via = "name_overlap"

    if template is None:
        return None

    try:
        pos_tokens, neg_tokens = _parse_style_text(template.get("text") or "")
    except Exception:
        pos_tokens, neg_tokens = [], []
    if not pos_tokens:
        return None
    return {
        "template_id": (template.get("id") or "").strip(),
        "name": (template.get("name") or "").strip() or "Style",
        "body_text": ", ".join(pos_tokens),
        "neg_tokens": neg_tokens,
        "matched_alias": matched_alias,
        "is_default": bool(hit and hit.get("is_default")),
        "via": via,
    }


# ── Scene match ────────────────────────────────────────────────


def _strip_scene_words(s: str) -> str:
    """Strip noise words decompose often leaves in scene intent text
    ('classroom scene', 'beach setting', 'in a tokyo location') so the
    lookup matches the KB display_name."""
    s = (s or "").strip().lower()
    s = re.sub(r"\b(scene|setting|location|place|background|environment|backdrop)s?\b",
               "", s)
    s = re.sub(r"\b(in|at|on|a|an|the|of)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_scene(conn: sqlite3.Connection, text: str) -> Optional[dict]:
    if not text:
        return None
    raw = text.strip().lower()
    norm = _normalize_token(text)
    stripped = _strip_scene_words(text)
    stripped_norm = _normalize_token(stripped) if stripped else ""
    # Exact item_tag (raw or stripped).
    for tag in (norm, stripped_norm):
        if not tag:
            continue
        row = conn.execute(
            "SELECT item_tag, display_name, base_natlang FROM scene_items "
            "WHERE item_tag = ? LIMIT 1",
            (tag,),
        ).fetchone()
        if row:
            return dict(row)
    # Display name exact (raw or stripped).
    for d in (raw, stripped):
        if not d:
            continue
        row = conn.execute(
            "SELECT item_tag, display_name, base_natlang FROM scene_items "
            "WHERE LOWER(display_name) = ? LIMIT 1",
            (d,),
        ).fetchone()
        if row:
            return dict(row)
    # Substring on display (prefer the stripped form for narrower match).
    for q in ((stripped or raw),):
        if not q:
            continue
        rows = conn.execute(
            "SELECT item_tag, display_name, base_natlang FROM scene_items "
            "WHERE LOWER(display_name) LIKE ? LIMIT 3",
            (f"%{q}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
    return None


# ── Resolve API ───────────────────────────────────────────────────


def resolve_intent(intent: dict, scan: Optional[dict] = None,
                   character_tag: Optional[str] = None,
                   model_hash: Optional[str] = None) -> dict:
    """Take a decompose intent and try to enrich its text with
    canonical content from the DB. Returns a dict with the same shape
    plus a `resolved` field.

      input:  {concept, op, text}, optional scan output for context,
              optional character_tag scoping outfit/pose/scene lookups
      output: {concept, op, text,            # unchanged
               resolved_text,                # canonical natlang or None
               resolved_source,              # 'character', 'outfit', 'scene', 'pose', None
               resolved_match}               # the matched row (for debugging)

    `character_tag` is the resolved character context from earlier in
    the same turn (build-mode subject intent or edit-mode character
    sentence). When provided, outfit/pose lookups scope to that
    character first; without it, they fall back to scan-derived
    character or fail open. This is how a build-mode `outfit=killer
    bee outfit` after `subject=cammy white` lands on Cammy's Killer
    Bee row instead of the empty result.
    """
    out = dict(intent)
    out["resolved_text"] = None
    out["resolved_source"] = None
    out["resolved_match"] = None

    concept = intent.get("concept", "")
    text = intent.get("text", "") or ""

    conn = _open_db()
    try:
        if concept == "subject":
            ch = match_character(conn, text)
            if ch:
                natlang = compose_character_natlang_v2(conn, ch["tag"])
                if not natlang:
                    # Fall back to legacy base_natlang column when V2
                    # chip composition is empty (older character rows
                    # have base_natlang text but no appearance_chip_tags
                    # populated). Without this, multi-char build skips
                    # the character entirely for any not-yet-chipped
                    # character — KB has the prose, just not the
                    # chip-decomposed form.
                    row = conn.execute(
                        "SELECT base_natlang FROM characters WHERE tag = ?",
                        (ch["tag"],),
                    ).fetchone()
                    legacy = (row["base_natlang"] or "").strip() if row else ""
                    if legacy:
                        out["resolved_text"] = legacy
                        out["resolved_source"] = "character"
                        out["resolved_match"] = ch
                        return out
                if natlang:
                    # Prepend the "<Display> from <Series>" intro the
                    # existing render_character_section uses.
                    display = ch["display"] or ch["tag"]
                    series = ch["series"] or ""
                    intro = (f"{display} from {series}, "
                             if series else f"{display}, ")
                    out["resolved_text"] = intro + natlang
                    out["resolved_source"] = "character"
                    out["resolved_match"] = ch
            return out

        # Whole-outfit concepts resolve to a complete outfit row (and
        # fall back to the character's default when the named outfit
        # isn't found). Sub-slot concepts are only allowed to resolve
        # to a different character's WHOLE outfit when the user said
        # something like "she's wearing chun-li's outfit" inside the
        # tops slot — we deliberately drop the character_tag scope
        # there so `match_outfit("barefoot")` doesn't fall through to
        # the current character's default and clobber the slot intent.
        whole_outfit_concepts = {"outfit", "outfit_swap"}
        subslot_concepts = {"tops", "bottoms", "footwear", "legwear",
                            "handwear", "armwear", "headwear", "neckwear",
                            "accessories"}
        if concept in whole_outfit_concepts or concept in subslot_concepts:
            # Sub-slots pass character_tag=None on purpose so the
            # default-fallback inside match_outfit doesn't fire for
            # bare item text. The sub-slot path is still allowed to
            # cross-resolve into another character's whole outfit
            # ("she's wearing chun-li's outfit" said inside `tops`).
            scope_tag = character_tag if concept in whole_outfit_concepts else None
            match = match_outfit(conn, text, character_tag=scope_tag)
            if match:
                if match["source"] == "outfit":
                    natlang = compose_outfit_natlang_v2(conn, match["outfit_id"])
                    if not natlang:
                        # Fall back to stored outfit_natlang.
                        row = conn.execute(
                            "SELECT outfit_natlang FROM outfits WHERE id = ?",
                            (match["outfit_id"],),
                        ).fetchone()
                        natlang = (row["outfit_natlang"] if row else "") or ""
                    if natlang:
                        out["resolved_text"] = natlang.strip()
                        out["resolved_source"] = "outfit"
                        out["resolved_match"] = match
                elif match["source"] == "generic":
                    out["resolved_text"] = (match.get("outfit_natlang") or "").strip()
                    out["resolved_source"] = "generic_outfit"
                    out["resolved_match"] = match
            return out

        if concept == "scene":
            sc = match_scene(conn, text)
            if sc:
                body = (sc.get("base_natlang") or "").strip()
                display = (sc.get("display_name") or "").strip()
                # A scene row is "thin" when its body is empty, equal
                # to its display name (e.g. Beach: base_natlang='beach',
                # display_name='Beach' — natlang_status='unprocessed'),
                # or shorter than ~20 chars. In those cases fall through
                # to vibe so the user still gets a coherent sentence
                # instead of just the bare scene name.
                is_thin = (
                    not body
                    or body.lower() == display.lower()
                    or len(body) < 20
                )
                if not is_thin:
                    out["resolved_text"] = body
                    out["resolved_source"] = "scene"
                    out["resolved_match"] = sc
            return out

        if concept == "pose":
            # Only resolve pose on replace/add ops; modifier or
            # anatomy-shaped pose intents pass through.
            op = intent.get("op", "")
            if op in ("replace", "add", ""):
                ps = match_pose(conn, text, character_tag=character_tag)
                if not ps and scan:
                    # Fall back: derive character from scan's character
                    # sentence. The full sentence is long and won't
                    # match a character display directly — pull just
                    # the leading name + series clause.
                    char_sentence = (scan.get("character") or "").strip()
                    name_clause = _extract_subject_name(char_sentence)
                    if name_clause:
                        ch = match_character(conn, name_clause)
                        if ch:
                            ps = match_pose(conn, text, character_tag=ch["tag"])
                if ps:
                    natlang = compose_pose_natlang_v2(conn, ps["pose_id"])
                    if natlang:
                        out["resolved_text"] = natlang.strip()
                        out["resolved_source"] = "pose"
                        out["resolved_match"] = ps
            return out

        if concept == "style":
            op = intent.get("op", "")
            if op in ("replace", "add", ""):
                st = match_style_template(text, model_hash=model_hash)
                if st:
                    out["resolved_text"] = st["body_text"]
                    out["resolved_source"] = "style"
                    out["resolved_match"] = st
            return out

        # expression — deferred
        return out
    finally:
        conn.close()


# ── Test fixtures ─────────────────────────────────────────────────


FIXTURES = [
    {
        "name": "subject_cammy_lowercase",
        "intent": {"concept": "subject", "op": "replace", "text": "cammy white"},
        "expect_source": "character",
        "expect_match_tag": "cammy_white",
    },
    {
        "name": "subject_cammy_firstname",
        "intent": {"concept": "subject", "op": "replace", "text": "cammy"},
        "expect_source": "character",
        "expect_match_tag": "cammy_white",
    },
    {
        "name": "subject_chunli_hyphenated",
        "intent": {"concept": "subject", "op": "replace", "text": "chun-li"},
        "expect_source": "character",
        "expect_match_tag": "chun-li",
    },
    {
        "name": "subject_chunli_spaced",
        "intent": {"concept": "subject", "op": "replace", "text": "chun li"},
        "expect_source": "character",
        "expect_match_tag": "chun-li",
    },
    {
        "name": "subject_unknown",
        "intent": {"concept": "subject", "op": "replace", "text": "definitely not a real character zzz"},
        "expect_source": None,
    },
    {
        "name": "outfit_chunli_default",
        "intent": {"concept": "outfit_swap", "op": "replace", "text": "chun li outfit"},
        "expect_source": "outfit",
    },
    {
        "name": "outfit_chunli_possessive",
        "intent": {"concept": "outfit_swap", "op": "replace", "text": "chun-li's outfit"},
        "expect_source": "outfit",
    },
    {
        "name": "outfit_in_tops_slot",
        # User says "she's wearing chun li's outfit" — concept ends up
        # as tops/bottoms/etc when decompose splits. We should still
        # resolve to chun-li's default outfit.
        "intent": {"concept": "tops", "op": "add", "text": "chun li's outfit"},
        "expect_source": "outfit",
    },
    {
        "name": "scene_specific",
        # Inspect — depends on what's in scene_items.
        "intent": {"concept": "scene", "op": "replace", "text": "beach"},
    },
    {
        "name": "outfit_generic_kimono",
        # Inspect — generic_outfits fallback.
        "intent": {"concept": "outfit_swap", "op": "replace", "text": "kimono"},
    },
    {
        "name": "pose_cammy_victory_possessive",
        "intent": {"concept": "pose", "op": "replace",
                   "text": "cammy white's victory pose"},
        "expect_source": "pose",
    },
    {
        "name": "pose_cammy_combat_stance",
        "intent": {"concept": "pose", "op": "replace",
                   "text": "cammy white's combat stance"},
        "expect_source": "pose",
    },
    {
        "name": "pose_no_character_context",
        # Just "victory pose" without naming a character — should NOT
        # match because we can't resolve to a specific character's
        # canonical pose.
        "intent": {"concept": "pose", "op": "replace",
                   "text": "victory pose"},
        "expect_source": None,
    },
    {
        "name": "pose_unknown_pose_name",
        # Character exists, pose name nonsense — no match.
        "intent": {"concept": "pose", "op": "replace",
                   "text": "cammy white's hopscotch shuffle"},
        "expect_source": None,
    },
]


def _hr(label: str = ""):
    bar = "=" * 78
    print(f"\n{bar}\n{label}\n{bar}")


def _block(label: str, body: str) -> None:
    print(f"--- {label} ---")
    for line in (body or "").splitlines():
        print(f"  {line}")
    if not body:
        print("  (empty)")


def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    selected = [f for f in FIXTURES if not name_filter or name_filter in f["name"]]
    print(f"resolve probe — {len(selected)} fixtures\n")
    pass_count = 0
    inspect_count = 0
    for fx in selected:
        _hr(f"{fx['name']}")
        print(f"  intent: {fx['intent']}")
        result = resolve_intent(fx["intent"])
        print(f"  resolved_source: {result['resolved_source']}")
        m = result.get("resolved_match")
        if m:
            # Print a compact summary of the match.
            keys = [k for k in ("tag", "display", "series", "character_tag",
                                 "outfit_name", "is_default", "item_tag",
                                 "display_name", "name") if k in m]
            print(f"  match: {{ " + ", ".join(f'{k}={m[k]!r}' for k in keys) + " }")
        _block("resolved_text", result["resolved_text"] or "")
        has_expect = any(k.startswith("expect_") for k in fx)
        if has_expect:
            failures = []
            if "expect_source" in fx:
                if result["resolved_source"] != fx["expect_source"]:
                    failures.append(
                        f"source={result['resolved_source']!r} expected {fx['expect_source']!r}"
                    )
            if "expect_match_tag" in fx:
                m = result.get("resolved_match") or {}
                tag = m.get("tag") or m.get("character_tag")
                if tag != fx["expect_match_tag"]:
                    failures.append(
                        f"match_tag={tag!r} expected {fx['expect_match_tag']!r}"
                    )
            if failures:
                print(f"  [FAIL]")
                for f in failures:
                    print(f"    ! {f}")
            else:
                print(f"  [PASS]")
                pass_count += 1
        else:
            print(f"  [INSPECT]")
            inspect_count += 1
    expected = sum(1 for f in selected
                   if any(k.startswith("expect_") for k in f.keys()))
    print(f"\n=== passed: {pass_count}/{expected} ; inspect-only: {inspect_count} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
