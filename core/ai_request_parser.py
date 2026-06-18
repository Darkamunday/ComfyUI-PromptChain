"""Parse user_request strings into structured Intents for the natlang
render flow.

This is the structural-intent extraction layer. The render flow uses
these intents to mutate PromptState deterministically (server-side),
then renders prose. Anything the parser doesn't recognize falls through
as `descriptive_residue` — passed to the model for prose elaboration.

Recognized intent kinds (corresponds to PromptState mutations):

  - outfit_strip:        "wearing only X" / "just X" / "only X"
  - outfit_swap:         "switch to X (outfit)" / "change outfit to X" / "in X outfit"
                         / "wearing X outfit"  (X must match a bio outfit name)
  - pose_swap:           "doing X (pose)" / "in X pose" / pose-name match
                         (X must match a bio pose name)
  - expression_set:      explicit `expression: X` form
  - setting_set:         "in a/at the X" + location keyword OR "setting: X"
  - modifier_apply:      any slot_modifier alias hit (delegates to
                         _detect_modifiers_in_text in ai_api)
  - pose_descriptive:    posture verb hit (sitting/kneeling/lying/...) OR
                         residue clause that classifies as pose-related.
                         Includes flag is_posture: True when the phrase
                         carries a posture verb (the render flow uses
                         this to override bio pose anchor).
  - expression_descriptive: residue clause classified as expression-related
                             OR plain emotion word.
  - setting_descriptive: residue clause classified as setting-related.
  - style_swap:          delegates to existing style_search.style_alias_scan
                         (handled outside this module, in ai_api)

The parser is intentionally regex/keyword-based, not LLM-driven. Speed
matters (sub-millisecond parsing), and the patterns are bounded. The
render flow consumes structural intents deterministically and uses the
descriptive intents to render Pose/Expression/Setting prose without a
model call."""
from __future__ import annotations

import re
from typing import Optional


# ── intent shapes ───────────────────────────────────────────────────
# Each Intent is a dict: {"kind": str, ...kind-specific fields...}
#   outfit_strip:    {kind, kept_slot: "legwear", target_item: "red_socks", color: "red"}
#   outfit_swap:     {kind, character_tag: "cammy_white", outfit_name: "Delta Red", outfit_id: int}
#   pose_swap:       {kind, character_tag, pose_name, pose_id, is_signature, natlang}
#   expression_set:  {kind, expression: "smiling"}
#   setting_set:     {kind, setting: "dungeon"}
#   modifier_apply:  {kind, modifier: dict (from _detect_modifiers_in_text)}
#   style_swap:      {kind, template_id, name, is_neutral, is_default}


# "wearing only X" / "just X" / "only X"
# Capture X up to a clause boundary so multi-intent prompts like
# "wearing only red socks, smiling, in a dungeon" don't swallow the
# downstream clauses into the strip target.
_STRIP_PATTERNS = [
    re.compile(r"\b(?:wearing|in)\s+only\s+(.+?)(?:\.|,|;|$)", re.IGNORECASE),
    re.compile(r"\bjust\s+(.+?)(?:\.|,|;|$)", re.IGNORECASE),
    re.compile(r"\bonly\s+(?:wearing\s+)?(.+?)(?:\.|,|;|$)", re.IGNORECASE),
]

# "switch to X outfit" / "change to X outfit" / "in X outfit" / "wearing X outfit"
# Anchored variants run first so "switch to delta red outfit" captures
# the full "delta red" rather than terminating the lazy match at the
# first word boundary. The trailing fallbacks run to clause boundary
# for "switch to X" without an outfit/costume/attire anchor.
_OUTFIT_SWAP_PATTERNS = [
    re.compile(r"\bswitch\s+(?:to|her\s+to)\s+(?:the\s+)?(.+?)\s+(?:outfit|costume|attire)\b", re.IGNORECASE),
    re.compile(r"\bchange\s+(?:outfit\s+to|to)\s+(?:the\s+)?(.+?)\s+(?:outfit|costume|attire)\b", re.IGNORECASE),
    re.compile(r"\bin\s+(?:the\s+|her\s+)?(.+?)\s+(?:outfit|costume|attire)\b", re.IGNORECASE),
    re.compile(r"\bwearing\s+(?:the\s+|her\s+)?(.+?)\s+(?:outfit|costume|attire)\b", re.IGNORECASE),
    re.compile(r"\bswitch\s+(?:to|her\s+to)\s+(?:the\s+)?(.+?)(?:\.|,|;|$)", re.IGNORECASE),
    re.compile(r"\bchange\s+(?:outfit\s+to|to)\s+(?:the\s+)?(.+?)(?:\.|,|;|$)", re.IGNORECASE),
]

# "doing X pose" / "in X pose" / "X pose" — anchored forms first so
# "doing spiral arrow pose" doesn't terminate the lazy capture at the
# first word boundary inside "spiral arrow". The bare "doing X" fallback
# runs to clause boundary.
_POSE_SWAP_PATTERNS = [
    re.compile(r"\bdoing\s+(?:the\s+|a\s+|her\s+)?(.+?)\s+(?:pose|stance)\b", re.IGNORECASE),
    re.compile(r"\bin\s+(?:the\s+|a\s+|her\s+)?(.+?)\s+(?:pose|stance)\b", re.IGNORECASE),
    re.compile(r"\b(.+?)\s+(?:pose|stance)\b", re.IGNORECASE),
    re.compile(r"\bdoing\s+(?:the\s+|a\s+|her\s+)?(.+?)(?:\.|,|;|$)", re.IGNORECASE),
]

# Expressions — small whitelist of single-word emotional descriptors.
# More phrasal forms ("with a smile", "looks sad") fall through to
# residue and the model handles them.
_EXPRESSION_KEYWORDS = {
    "smiling", "smile", "smirking", "smirk",
    "frowning", "frown", "scowling", "scowl", "glaring",
    "crying", "tearful", "weeping",
    "blushing", "embarrassed", "flushed",
    "laughing", "giggling",
    "angry", "furious", "annoyed",
    "neutral", "stoic", "deadpan",
    "shocked", "surprised", "wide-eyed",
    "sleepy", "drowsy", "tired",
    "happy", "joyful", "ecstatic",
    "sad", "melancholy", "depressed",
    "confident", "smug", "proud",
}

# Setting keywords. Match "in a/at the X" where X contains one of these.
# Plus "setting: X" / "scene: X" / "background: X".
_SETTING_KEYWORDS = {
    "dungeon", "forest", "beach", "desert", "city", "alley", "rooftop",
    "bedroom", "bathroom", "kitchen", "office", "classroom", "library",
    "cafe", "bar", "club", "stage", "studio", "park", "garden",
    "battlefield", "ruins", "temple", "shrine", "castle", "tower",
    "spaceship", "laboratory", "factory", "warehouse", "garage",
    "mountain", "river", "lake", "waterfall", "cave", "grove",
    "street", "sidewalk", "balcony", "courtyard", "field",
    "underground", "rooftop", "rain", "snow", "fog", "night", "dawn",
    "dusk", "sunset", "sunrise",
}

_SETTING_EXPLICIT_RE = re.compile(
    r"\b(?:setting|scene|background|location|environment)\s*:\s*(.+?)(?:\.|,|;|$)",
    re.IGNORECASE,
)
_SETTING_PHRASE_RE = re.compile(
    r"\b(?:in|at|on)\s+(?:a|an|the)?\s*([\w \-]+?)(?:\.|$|,)",
    re.IGNORECASE,
)

# Posture verbs — their presence in residue indicates a pose CHANGE
# (overrides any bio-matched pose anchor). Distinct from named-pose swap
# because no bio match is required; the descriptive phrase is the pose.
_POSTURE_VERBS = frozenset({
    "sitting", "seated",
    "standing", "stood",
    "kneeling", "kneeled",
    "lying", "laying", "lay", "reclining",
    "crouching", "crouched", "squatting",
    "jumping", "leaping",
    "running", "sprinting",
    "walking", "strolling", "striding",
    "bending", "hunched", "leaning",
    "stretching", "arching",
    "dancing",
    "fighting", "punching", "kicking",
    "posing",
})

# Pose-bin keywords: words that mean a residue clause is about pose/action
# (anatomy, gaze, action verbs). Used by residue binner when no posture
# verb fired. Keep narrow — broad matches over-bin into pose.
_POSE_BIN_KEYWORDS = frozenset({
    "legs", "leg", "arms", "arm", "hands", "hand",
    "feet", "foot", "toes", "toe",
    "knees", "knee", "elbows", "elbow", "shoulders", "shoulder",
    "hips", "hip", "back", "chest",
    "viewer", "camera",
    "showing", "shows", "show", "presenting", "presents",
    "holding", "holds", "gripping", "grasping",
    "reaching", "pointing", "waving",
    "looking", "looks", "gazing", "staring",
    "facing", "facing-away", "turned",
})

# Small whitelist of "dressed-state" verbs we don't want classified as
# pose. "wearing X" should NEVER bin to pose — it's outfit territory.
_OUTFIT_BIN_KEYWORDS = frozenset({
    "wearing", "wears", "dressed", "clad", "donning",
})


def _normalize(text: str) -> str:
    return (text or "").strip()


def _collect_outfit_names(bios: list[dict] | None) -> dict[str, dict]:
    """Map lowercase outfit name -> {character_tag, outfit_name, outfit_id,
    is_default, slots, natlang} for fuzzy bio outfit lookup. We use the
    bio's pre-loaded list (set at /tag-builder/match-characters time)
    rather than re-querying the DB."""
    out: dict[str, dict] = {}
    for b in bios or []:
        char_tag = b.get("tag", "")
        # Each bio carries either default_outfit OR user_requested_outfit
        # but the OutfitSwap intent must match against the FULL list of
        # named outfits the character has. The /match-characters endpoint
        # currently only returns the picked one. We fall back to whatever
        # is on the bio dict — if a richer list is needed, the bio shape
        # gets extended. For the parser's purposes, the picked outfit is
        # enough as a starting point; the render flow re-picks from the
        # DB once it knows the new outfit_name.
        for outfit_field in ("user_requested_outfit", "default_outfit"):
            o = b.get(outfit_field)
            if not o:
                continue
            name = (o.get("name") or "").strip()
            if name:
                out[name.lower()] = {
                    "character_tag": char_tag,
                    "outfit_name": name,
                    "outfit_id": o.get("id"),
                    "slots": o.get("slots") or [],
                    "natlang": o.get("natlang") or "",
                    "tags": o.get("tags") or "",
                }
    return out


def _collect_pose_names(bios: list[dict] | None) -> dict[str, dict]:
    """Map lowercase pose name -> {character_tag, pose_name, pose_id,
    is_signature, natlang, tags} for every pose curated for every
    character in `bios`.

    Iterates `all_poses` (the full per-character pose list shipped by
    match-characters) so the parser sees every candidate the user could
    plausibly name — required for the bge-small semantic fallback in
    _lookup_bio_pose_in_text to find paraphrases (`fighting pose` →
    `Combat Stance`) that aren't substring-matchable. Falls back to
    `matched_pose` alone for legacy bio shapes."""
    out: dict[str, dict] = {}
    for b in bios or []:
        char_tag = b.get("tag", "")
        all_poses = b.get("all_poses") or []
        if all_poses:
            for pose in all_poses:
                name = (pose.get("name") or "").strip()
                if not name:
                    continue
                out.setdefault(name.lower(), {
                    "character_tag": char_tag,
                    "pose_name": name,
                    "pose_id": pose.get("id"),
                    "is_signature": bool(pose.get("is_signature")),
                    "natlang": pose.get("natlang") or "",
                    "tags": pose.get("tags") or "",
                })
            continue
        pose = b.get("matched_pose")
        if not pose:
            continue
        name = (pose.get("name") or "").strip()
        if name:
            out[name.lower()] = {
                "character_tag": char_tag,
                "pose_name": name,
                "pose_id": pose.get("id"),
                "is_signature": bool(pose.get("is_signature")),
                "natlang": pose.get("natlang") or "",
                "tags": pose.get("tags") or "",
            }
    return out


def _try_parse_strip(text: str) -> Optional[dict]:
    """Match 'wearing only X' / 'just X' / 'only X' — return the X part.
    Caller resolves which slot the X fills. Returns None if no match."""
    for pat in _STRIP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        target = _normalize(m.group(1))
        if not target:
            continue
        return {"kind": "outfit_strip", "target_phrase": target}
    return None


def parse_intents(user_request: str,
                  bios: list[dict] | None) -> dict:
    """Return {intents: list[Intent], descriptive_residue: str}.

    Detection order (each layer consumes char spans from the residue):
      1. outfit_strip      — most specific structural verb
      2. outfit_swap       — only if no strip
      3. pose_swap         — bio-matched pose name
      4. modifier_apply    — slot_modifier alias scan (multiple may fire)
      5. expression_set    — explicit `expression: X` form
      6. setting_set       — explicit `setting: X` or `in/at/on <kw>` form
      7. posture verbs     — bare verbs (`sitting`, `kneeling`, ...)
      8. residue binning   — split remaining text into clauses, classify
                             each into pose / expression / setting via
                             keyword. Emits *_descriptive intents.

    Residue is what's left after layers 1-7 consume their spans. Layer 8
    classifies and emits descriptive intents — none of those go through
    the model. The render flow composes Pose/Expression/Setting prose
    from these intents deterministically."""
    text = _normalize(user_request)
    if not text:
        return {"intents": [], "descriptive_residue": ""}

    intents: list[dict] = []
    consumed: list[tuple[int, int]] = []

    outfit_lookup = _collect_outfit_names(bios)
    pose_lookup = _collect_pose_names(bios)

    # 1. Strip
    strip = _try_parse_strip(text)
    if strip:
        intents.append(strip)
        for pat in _STRIP_PATTERNS:
            m = pat.search(text)
            if m:
                consumed.append((m.start(), m.end()))
                break

    # 2. Outfit swap. Strip + swap is NOT contradictory — "in delta red
    # outfit wearing only red socks" means SWAP to Delta Red and THEN
    # strip down to socks. The applier runs swap before strip per the
    # delta ordering rule, so the strip's kept_slot survives the swap.
    swap_match = _find_outfit_swap_with_span(text, outfit_lookup)
    if swap_match:
        intent, span = swap_match
        # Skip if the swap span is fully inside an already-consumed strip
        # span (defensive — strip pattern shouldn't overlap, but the
        # `in only` form could).
        s_start, s_end = span
        already_consumed = any(c[0] <= s_start and c[1] >= s_end for c in consumed)
        if not already_consumed:
            intents.append(intent)
            consumed.append(span)

    # 3. Pose swap
    pose_match = _find_pose_swap_with_span(text, pose_lookup)
    if pose_match:
        intent, span = pose_match
        intents.append(intent)
        consumed.append(span)

    # 4. Modifier alias scan (lazy import to avoid module-level cycle
    # with ai_api). Each modifier is a separate intent. Match spans are
    # consumed so the alias text doesn't get re-binned to pose later.
    for mod_intent, span in _detect_modifier_intents(text):
        intents.append(mod_intent)
        if span:
            consumed.append(span)

    # 5. Explicit expression form (`expression: X`)
    expr_explicit = _try_parse_expression_explicit(text)
    if expr_explicit:
        intent, span = expr_explicit
        intents.append(intent)
        consumed.append(span)

    # 6. Explicit setting forms (`setting: X` or `in/at/on <kw>`)
    setting_explicit = _try_parse_setting_explicit(text)
    if setting_explicit:
        intent, span = setting_explicit
        intents.append(intent)
        consumed.append(span)

    # 7. Posture verbs — bare detection. Each match is a pose_descriptive
    # intent flagged as posture (overrides bio pose anchor). Pass bio
    # display names so the leading character mention is stripped, plus
    # the already-consumed spans so the posture clause doesn't bleed
    # into outfit_swap / modifier_apply text the upstream layers already
    # claimed.
    bio_names_lc = {(b.get("display") or b.get("tag") or "").lower()
                    for b in (bios or []) if b}
    bio_names_lc.discard("")
    for posture_intent, span in _detect_posture_intents(text, bio_names_lc, consumed):
        intents.append(posture_intent)
        if span:
            consumed.append(span)

    # 8. Residue binning. Take what's left and split into clauses; each
    # clause classified into pose/expression/setting via keyword scan.
    residue = _residue_from_consumed(text, consumed)
    bin_intents, leftover = _bin_residue_to_intents(residue, bios)
    intents.extend(bin_intents)

    return {"intents": intents, "descriptive_residue": leftover}


# ── span-aware structural finders ──────────────────────────────────

def _find_outfit_swap_with_span(text: str,
                                 outfit_lookup: dict[str, dict]
                                 ) -> Optional[tuple[dict, tuple[int, int]]]:
    """Match an outfit-swap phrasing and return (intent, span). Emits a
    candidate intent even when the named outfit isn't in the preloaded
    bio cache — the render flow re-picks from the DB via _pick_outfit_for
    once it knows the new outfit_name. Without this fallback, "switch to
    delta red outfit" would silently drop when bio carried only Killer Bee."""
    for pat in _OUTFIT_SWAP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = _normalize(m.group(1))
        if not candidate:
            continue
        candidate_lc = candidate.lower()
        if candidate_lc in outfit_lookup:
            hit = outfit_lookup[candidate_lc]
            return ({"kind": "outfit_swap", **hit}, (m.start(), m.end()))
        for name_lc, hit in outfit_lookup.items():
            if name_lc in candidate_lc or candidate_lc in name_lc:
                return ({"kind": "outfit_swap", **hit}, (m.start(), m.end()))
        return ({"kind": "outfit_swap",
                 "character_tag": "",
                 "outfit_name": candidate,
                 "outfit_id": None,
                 "slots": [],
                 "natlang": "",
                 "tags": ""}, (m.start(), m.end()))
    return None


def _find_pose_swap_with_span(text: str,
                               pose_lookup: dict[str, dict]
                               ) -> Optional[tuple[dict, tuple[int, int]]]:
    """Match pose-swap phrasing. Emits a candidate intent even when the
    named pose isn't in the preloaded bio cache — render flow re-picks
    via _pick_pose_for over the full DB once it knows the pose name."""
    for pat in _POSE_SWAP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = _normalize(m.group(1))
        if not candidate or len(candidate) < 3:
            continue
        candidate_lc = candidate.lower()
        if candidate_lc in pose_lookup:
            return ({"kind": "pose_swap", **pose_lookup[candidate_lc]}, (m.start(), m.end()))
        for name_lc, hit in pose_lookup.items():
            if name_lc in candidate_lc or candidate_lc in name_lc:
                return ({"kind": "pose_swap", **hit}, (m.start(), m.end()))
        return ({"kind": "pose_swap",
                 "character_tag": "",
                 "pose_name": candidate,
                 "pose_id": None,
                 "is_signature": False,
                 "natlang": "",
                 "tags": ""}, (m.start(), m.end()))
    return None


# ── modifier alias detection ───────────────────────────────────────

def _detect_modifier_intents(text: str) -> list[tuple[dict, Optional[tuple[int, int]]]]:
    """Wrap _detect_modifiers_in_text from ai_api and return modifier_apply
    intents with their matched spans. Lazy import to dodge circular
    module load order."""
    try:
        from .ai_api import _detect_modifiers_in_text
    except Exception:
        return []
    detected = _detect_modifiers_in_text(text) or []
    out: list[tuple[dict, Optional[tuple[int, int]]]] = []
    for mod in detected:
        alias = (mod.get("matched_alias") or "").strip()
        span = None
        if alias:
            m = re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)",
                          text, re.IGNORECASE)
            if m:
                span = (m.start(), m.end())
        out.append(({"kind": "modifier_apply", "modifier": mod}, span))
    return out


# ── explicit expression / setting (kept separate from descriptive) ──

def _try_parse_expression_explicit(text: str
                                    ) -> Optional[tuple[dict, tuple[int, int]]]:
    """Match `expression: X` only. Plain emotion words flow through
    residue binning so multi-section prompts ("smiling, sitting") don't
    eat the whole text."""
    m = re.search(r"\bexpression\s*:\s*(.+?)(?:\.|$)", text, re.IGNORECASE)
    if not m:
        return None
    expr = _normalize(m.group(1))
    if not expr:
        return None
    return ({"kind": "expression_set", "expression": expr}, (m.start(), m.end()))


def _try_parse_setting_explicit(text: str
                                 ) -> Optional[tuple[dict, tuple[int, int]]]:
    """Match `setting: X` / `scene: X` / `background: X`. Plain `in a forest`
    forms flow through residue binning."""
    m = _SETTING_EXPLICIT_RE.search(text)
    if not m:
        return None
    val = _normalize(m.group(1))
    if not val:
        return None
    return ({"kind": "setting_set", "setting": val}, (m.start(), m.end()))


# ── posture verb detection ─────────────────────────────────────────

def _detect_posture_intents(text: str,
                             bio_display_names: set[str] | None = None,
                             already_consumed: list[tuple[int, int]] | None = None,
                             ) -> list[tuple[dict, tuple[int, int]]]:
    """Find posture verbs and return pose_descriptive intents. Each
    intent carries the clause around the verb (so "sitting with legs
    up" stays together) and is flagged is_posture=True so the render
    flow knows to override bio pose anchor.

    bio_display_names: lowercase character names. Stripped from the
    leading edge of the clause to avoid pose prose reading like
    "cammy white sitting...". Bio retrieval already handles character
    identity — pose prose should describe the action only.

    already_consumed: char-spans the upstream layers already claimed
    (outfit_swap, outfit_strip, modifier_apply). Posture clause
    expansion stops at any consumed-span boundary so "cammy white in
    her killer bee outfit sitting with legs up with barefeet" doesn't
    yield a pose phrase containing "in her killer bee outfit"."""
    names = bio_display_names or set()
    consumed = already_consumed or []
    out: list[tuple[dict, tuple[int, int]]] = []
    for verb in _POSTURE_VERBS:
        for m in re.finditer(r"(?<!\w)" + re.escape(verb) + r"(?!\w)",
                             text, re.IGNORECASE):
            # Skip matches inside an already-consumed span.
            verb_pos = m.start()
            if any(s <= verb_pos < e for s, e in consumed):
                continue
            clause_start, clause_end = _expand_to_clause_with_consumed(
                text, m.start(), m.end(), consumed,
            )
            phrase = _normalize(text[clause_start:clause_end])
            if not phrase:
                continue
            phrase_lc = phrase.lower()
            for name in names:
                if name and (phrase_lc.startswith(name + " ") or phrase_lc == name):
                    phrase = phrase[len(name):].strip(" ,.;:")
                    phrase_lc = phrase.lower()
            # Trim outfit-verb tail. "sitting with legs up only wearing
            # red socks" should yield pose phrase "sitting with legs up";
            # the strip/fill side already consumed "only wearing red socks"
            # via _STRIP_PATTERNS. Without this trim, the whole clause
            # echoes into the pose section.
            tail_match = re.search(
                r"\s+(?:only\s+wearing|only\s+in|wearing|wears|dressed|clad|donning)\b",
                phrase, flags=re.IGNORECASE,
            )
            if tail_match:
                phrase = phrase[:tail_match.start()].strip(" ,.;:")
                if not phrase:
                    continue
            # Strip dangling prepositions ("sitting with legs up with" →
            # "sitting with legs up"). Happens when an upstream consumer
            # ate a noun phrase and left a connector preposition behind.
            phrase = re.sub(r"\s+(?:with|in|at|on|by|to|from)\s*$",
                            "", phrase, flags=re.IGNORECASE).strip(" ,.;:")
            if not phrase:
                continue
            out.append((
                {"kind": "pose_descriptive",
                 "phrase": phrase,
                 "is_posture": True},
                (clause_start, clause_end),
            ))
            break
    return out


def _expand_to_clause_with_consumed(text: str, start: int, end: int,
                                     consumed: list[tuple[int, int]]
                                     ) -> tuple[int, int]:
    """Like _expand_to_clause but also stops at consumed-span boundaries.
    Prevents posture clause from absorbing text already claimed by
    outfit_swap / strip / modifier_apply."""
    BOUNDARY = ",;.!?\n"
    s = start
    while s > 0 and text[s - 1] not in BOUNDARY:
        # Check if we'd cross into a consumed span; stop at its end edge.
        candidate = s - 1
        if any(c_start <= candidate < c_end for c_start, c_end in consumed):
            break
        s -= 1
    e = end
    while e < len(text) and text[e] not in BOUNDARY:
        if any(c_start <= e < c_end for c_start, c_end in consumed):
            break
        e += 1
    return s, e


# ── residue binning ────────────────────────────────────────────────

def _residue_from_consumed(text: str,
                            consumed: list[tuple[int, int]]) -> str:
    """Subtract consumed spans from text. Merges overlapping ranges,
    joins surviving slices with single spaces."""
    if not consumed:
        return text
    consumed_sorted = sorted(consumed)
    merged: list[tuple[int, int]] = []
    for s, e in consumed_sorted:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    parts: list[str] = []
    cursor = 0
    for s, e in merged:
        if s > cursor:
            parts.append(text[cursor:s])
        cursor = max(cursor, e)
    if cursor < len(text):
        parts.append(text[cursor:])
    return " ".join(p.strip() for p in parts if p.strip())


def _bin_residue_to_intents(residue: str,
                             bios: list[dict] | None
                             ) -> tuple[list[dict], str]:
    """Split residue into clauses and classify each into pose / expression
    / setting via keyword scan. Emits *_descriptive intents.

    Classification priority (per clause):
      1. setting keyword  -> setting_descriptive
      2. expression keyword -> expression_descriptive
      3. pose-bin keyword OR posture verb -> pose_descriptive
      4. otherwise         -> dropped (or kept in leftover)

    Character-name mentions and outfit phrasing ("wearing X") are
    skipped — they belong to the bio retrieval pipeline, not pose prose."""
    if not residue:
        return [], ""
    bio_display_names = {(b.get("display") or b.get("tag") or "").lower()
                         for b in (bios or []) if b}
    bio_display_names.discard("")
    intents: list[dict] = []
    leftover_parts: list[str] = []
    for clause in _split_clauses(residue):
        c = clause.strip(" ,.;:")
        if not c:
            continue
        c_lc = c.lower()
        # Drop pure character-name mentions ("cammy white").
        if c_lc in bio_display_names:
            continue
        # Drop pure character-name + nothing else by stripping the leading
        # name from the clause.
        for name in bio_display_names:
            if c_lc.startswith(name + " ") or c_lc.startswith(name + ","):
                c = c[len(name):].strip(" ,.;:")
                c_lc = c.lower()
                if not c:
                    break
        if not c:
            continue
        # Outfit-bin keywords ("wearing X") — split the clause at the
        # outfit verb so the pose prefix survives. "legs up wearing red
        # socks" → bin "legs up" as pose, "wearing red socks" goes to
        # leftover for the FillSlotDelta extractor downstream.
        if any(_word_in(c_lc, kw) for kw in _OUTFIT_BIN_KEYWORDS):
            split_match = re.search(
                r"\b(wearing|wears|dressed|clad|donning)\b",
                c, flags=re.IGNORECASE,
            )
            if split_match and split_match.start() > 0:
                pose_prefix = c[:split_match.start()].strip(" ,.;:")
                outfit_tail = c[split_match.start():].strip(" ,.;:")
                if pose_prefix:
                    pose_prefix_lc = pose_prefix.lower()
                    is_posture_clause = any(_word_in(pose_prefix_lc, v) for v in _POSTURE_VERBS)
                    if is_posture_clause or any(_word_in(pose_prefix_lc, kw) for kw in _POSE_BIN_KEYWORDS):
                        intents.append({
                            "kind": "pose_descriptive",
                            "phrase": pose_prefix,
                            "is_posture": is_posture_clause,
                        })
                    else:
                        leftover_parts.append(pose_prefix)
                if outfit_tail:
                    leftover_parts.append(outfit_tail)
                continue
            leftover_parts.append(c)
            continue
        # Setting keyword wins (locations imply scene context). Word-
        # boundary match — substring match would mis-bin "barefoot"
        # (contains "bar") and other modifier aliases as settings.
        if any(_word_in(c_lc, kw) for kw in _SETTING_KEYWORDS):
            intents.append({"kind": "setting_descriptive", "phrase": c})
            continue
        # Expression keyword.
        if any(_word_in(c_lc, kw) for kw in _EXPRESSION_KEYWORDS):
            intents.append({"kind": "expression_descriptive", "phrase": c})
            continue
        # Pose-bin keyword OR posture verb.
        is_posture_clause = any(_word_in(c_lc, v) for v in _POSTURE_VERBS)
        if is_posture_clause or any(_word_in(c_lc, kw) for kw in _POSE_BIN_KEYWORDS):
            intents.append({
                "kind": "pose_descriptive",
                "phrase": c,
                "is_posture": is_posture_clause,
            })
            continue
        # Unclassified — keep in leftover for logging / future LLM fallback.
        leftover_parts.append(c)
    return intents, ", ".join(leftover_parts)


def _split_clauses(text: str) -> list[str]:
    """Split on commas, semicolons, ` and `, ` with `, periods. Keeps
    sub-clauses small enough to classify cleanly. We don't try to be
    grammatical — multiple short clauses bin better than one long one."""
    parts = re.split(r"\s*(?:,|;|\band\b|\bwith\b|\.|\?|!)\s*",
                     text, flags=re.IGNORECASE)
    return [p for p in parts if p and p.strip()]


def _word_in(text: str, word: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text))
